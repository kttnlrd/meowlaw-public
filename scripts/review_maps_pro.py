#!/usr/bin/env python3
"""DeepSeek V4 Pro technical review of all procedural maps.

Enforces the subsection-completeness gate: every operative subsection of the
core section present, every diverting condition an explicit decision, every
edge case a facet. Strict JSON out per map -> data/map_reviews/{id}.json
"""
import json, os, re, sys, time, glob, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAPS_DIR = ROOT / "data" / "maps"
OUT_DIR = ROOT / "data" / "map_reviews"
OUT_DIR.mkdir(exist_ok=True)

KEY = os.environ.get("DEEPSEEK_API_KEY", "")
URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-v4-pro"
MAX_TOKENS = 65536

SYSTEM = """You are a senior Australian tax counsel reviewing procedural knowledge maps for a legislation explorer used by tax practitioners. Each map is a directed graph of nodes {id, type: start/event/decision/action/outcome/end, label, body, statute, commentary, rulings, cases} and edges {from, to, label}.

Review against the ACTUAL statute text supplied. Enforce the subsection-completeness gate:
1. Every operative subsection of the core section must appear — as a spine node where it drives the flow, or as a FACET node where it is a condition that could end or divert the line of reasoning ('if you have this, you must reconsider').
2. Every condition that can change the outcome must be an explicit decision node on the spine — not buried in a body text.
3. Every edge case must be captured as a facet. A facet is an end-of-line outcome node that flags a scenario where the main path does not apply.
4. Logic flow order must be right: elements checked in the order a practitioner would analyse them; carve-outs/exceptions AFTER the element they qualify.
5. Legal accuracy of every body: holdings, section references, definitions, consequences. Flag anything that contradicts the supplied statute text.
6. Signposting: labels must be questions or imperatives, not vague statements.

Return STRICT JSON only — no markdown fences, no commentary outside the JSON:
{"map_id": "...", "verdict": "pass"|"needs_fix", "issues": [{"node_id": "...", "severity": "critical"|"major"|"minor", "finding": "...", "suggested_fix": "..."}]}

Rules: node_id must be an actual node id in the map (use "map" for map-level issues). critical = wrong law or would mislead a practitioner; major = missing condition/facet that changes an outcome; minor = wording, signposting, or completeness polish. If the map is complete and accurate, verdict pass with an empty issues array. Be rigorous — this is the quality gate for a professional tool."""

def call_api(prompt, attempt=0):
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.loads(r.read().decode())
        content = data["choices"][0]["message"]["content"]
        return content
    except Exception as e:
        if attempt < 2:
            time.sleep(20 * (attempt + 1))
            return call_api(prompt, attempt + 1)
        raise

def extract_json(content):
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
    return json.loads(content)

def core_sections(map_data):
    """Pick up to 3 core sections: most-referenced statute refs."""
    from collections import Counter
    cnt = Counter()
    anchor = None
    for n in map_data.get("nodes", []):
        for s in n.get("statute", []):
            cnt[(s["act"], s["section"])] += 1
            if n["type"] == "start" and anchor is None:
                anchor = (s["act"], s["section"])
    ordered = [anchor] if anchor else []
    for k, _ in cnt.most_common():
        if k not in ordered:
            ordered.append(k)
        if len(ordered) >= 3:
            break
    return ordered

def load_section_text(act, section):
    pat = section.upper().replace("(", "").replace(")", "")
    for f in glob.glob(str(ROOT / "data" / act / "sections" / "**" / "*.md"), recursive=True):
        txt = Path(f).read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^section:\s*\"?([^\"]+?)\"?\s*$", txt, re.M)
        if m and m.group(1).upper() == section.upper():
            return txt
    return None

def review_map(path):
    map_data = json.loads(Path(path).read_text(encoding="utf-8"))
    mid = map_data["id"]
    # strip huge unrelated fields to keep prompt tight
    slim = {k: v for k, v in map_data.items() if k in ("id","title","act","part","division","subdivision","summary","short","refs","nodes","edges")}
    sec_texts = {}
    for act, sec in core_sections(map_data):
        t = load_section_text(act, sec)
        if t:
            sec_texts[f"{act}:{sec}"] = t
    prompt = json.dumps({
        "map": slim,
        "core_section_texts": sec_texts,
    }, ensure_ascii=False, indent=1)
    out = call_api(prompt)
    review = extract_json(out)
    if review.get("map_id") != mid:
        review["map_id"] = mid
    (OUT_DIR / f"{mid}.review.json").write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
    n = len(review.get("issues", []))
    sev = {"critical": 0, "major": 0, "minor": 0}
    for i in review.get("issues", []):
        sev[i.get("severity","minor")] = sev.get(i.get("severity","minor"), 0) + 1
    print(f"[{mid}] {review.get('verdict','?')} — {n} issues ({sev})", flush=True)
    return review

def main():
    maps = sorted(MAPS_DIR.glob("*.json"))
    # Resume: skip maps with a completed review on disk
    todo = [p for p in maps if not (OUT_DIR / f"{p.stem}.review.json").exists()]
    print(f"reviewing {len(todo)}/{len(maps)} maps with {MODEL}", flush=True)
    summary = []
    for i, p in enumerate(todo, 1):
        mid = p.stem
        print(f"[{i}/{len(todo)}] {mid} ...", flush=True)
        try:
            r = review_map(p)
            summary.append({"map_id": mid, "verdict": r.get("verdict"), "issues": len(r.get("issues", [])),
                            "critical": sum(1 for x in r.get("issues",[]) if x.get("severity")=="critical"),
                            "major": sum(1 for x in r.get("issues",[]) if x.get("severity")=="major")})
        except Exception as e:
            print(f"[{mid}] ERROR {e}", flush=True)
            summary.append({"map_id": mid, "verdict": "error", "issues": -1, "critical": -1, "major": -1})
        time.sleep(4)
    (OUT_DIR / "REVIEW_SUMMARY.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print("DONE", flush=True)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3.12
"""Apply the verified operator insertions to the corpus, in blocks, with a provenance invariant.

Only sections proven by scripts/verify_operator_credit.py are touched (A accepted, B rejected,
C rejected).  Every changed line must satisfy:

    remove the inserted pieces from the proposed line  ==  the original line, byte for byte

so nothing but the operators can have changed - no whitespace, no reflow, no dropped character.  A
line where a piece is ambiguous (occurs more than once) is refused rather than guessed.  Character
level invariant across the whole run: every character added is an operator the resolver read off the
page, and nothing else.

  --plan     print what would change (default: nothing is written)
  --apply    write it, block by block, and report per block
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROPOSAL = Path("/tmp/opres2/proposal.json")
VERIFY = Path("/tmp/apply-full/credit_verification.json")
RECORD = Path("/tmp/apply-full/operator_apply_record.json")

OPERATORS = set("×÷−–—+=-≤≥±<>,*")


def pieces(line: dict) -> list[str]:
    out = []
    for ins in sorted(line.get("insertions", []), key=lambda i: i["index"]):
        if ins.get("skipped"):
            continue
        out.append(ins.get("pad_before", "") + ins["char"] + ins.get("pad_after", ""))
    return out


def residue(proposed: str, current: str, line: dict) -> tuple[str | None, str]:
    """Prove the proposed line differs from the original ONLY by the inserted pieces.

    The insertion's index is an offset into the CURRENT line, so each piece is located at
    index + the length of the pieces already inserted before it.  Locating by index rather than by
    search is what makes a repeated piece unambiguous: a line can legitimately already contain the
    same character the page draws once more.
    """
    ins = [i for i in sorted(line.get("insertions", []), key=lambda x: x["index"])
           if not i.get("skipped")]
    shift = 0
    for i in ins:
        piece = i.get("pad_before", "") + i["char"] + i.get("pad_after", "")
        pos = i["index"] + shift
        if proposed[pos:pos + len(piece)] != piece:
            return None, (f"insertion at index {i['index']} does not sit at {pos} in the proposed "
                          f"line (found {proposed[pos:pos + len(piece)]!r}, expected {piece!r})")
        shift += len(piece)
    s, shift = proposed, 0
    for i in ins:
        piece = i.get("pad_before", "") + i["char"] + i.get("pad_after", "")
        pos = i["index"] + shift
        s = s[:pos] + s[pos + len(piece):]
    return s, ""


def plan() -> tuple[list[dict], list[dict]]:
    proposal = json.loads(PROPOSAL.read_text())
    verdicts = {r["section"]: r for r in json.loads(VERIFY.read_text())}
    fences: dict[str, list[dict]] = {}
    for f in proposal["fences"]:
        if f.get("status") == "change-needed":
            fences.setdefault(f["section"], []).append(f)

    todo, refused = [], []
    for section, fs in sorted(fences.items()):
        v = verdicts.get(section)
        if not v or not v.get("ok"):
            refused.append({"section": section, "why": "not proven by the credit verification",
                            "verdict": (v or {}).get("A", "missing")})
            continue
        src = Path(fs[0]["file"])
        if not src.is_absolute():
            src = ROOT / src
        for fence in fs:
            body = fence.get("body") or []
            for idx, ln in enumerate(fence.get("lines", [])):
                if ln.get("status") != "change-needed" or ln.get("proposed") == ln.get("current"):
                    continue
                cur, prop = ln["current"], ln["proposed"]
                md_line = body[idx].get("md_line") if idx < len(body) else None
                pc = pieces(ln)
                if not md_line:
                    refused.append({"section": section, "line": cur[:60],
                                    "why": "no md_line for this fence line - refusing rather than "
                                           "matching by text, which is ambiguous when a section "
                                           "repeats a line"})
                    continue
                if not pc:
                    refused.append({"section": section, "line": cur[:60],
                                    "why": "no insertions recorded for a change-needed line"})
                    continue
                back, why = residue(prop, cur, ln)
                if back is None:
                    refused.append({"section": section, "line": cur[:60], "why": why})
                    continue
                if back != cur:
                    refused.append({"section": section, "line": cur[:60],
                                    "why": "removing the inserted pieces does not reproduce the "
                                           "original line - more than the operator would change",
                                    "residue": back[:80], "original": cur[:80]})
                    continue
                added = Counter(c for c in prop if c not in Counter(cur))
                todo.append({"section": section, "file": str(src.relative_to(ROOT)),
                             "line_no": md_line, "current": cur, "proposed": prop, "pieces": pc,
                             "chars_added": dict(added)})
    return todo, refused


def apply_block(block: list[dict]) -> dict:
    """Write each line back at the exact line number the probe recorded it at."""
    by_file: dict[Path, list[dict]] = {}
    for item in block:
        by_file.setdefault(ROOT / item["file"], []).append(item)
    written = 0
    for path, items in by_file.items():
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for item in items:
            i = item["line_no"] - 1
            if i >= len(lines) or lines[i].rstrip("\n") != item["current"]:
                got = lines[i].rstrip("\n") if i < len(lines) else "<past end of file>"
                raise SystemExit(f"{path}:{item['line_no']} does not hold the expected line\n"
                                 f"  expected {item['current'][:70]!r}\n  found    {got[:70]!r}")
            lines[i] = item["proposed"] + "\n"
            written += 1
        path.write_text("".join(lines), encoding="utf-8")
    return {"lines_written": written, "files": len(by_file)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--block-size", type=int, default=10)
    a = ap.parse_args()

    todo, refused = plan()
    added = Counter()
    for t in todo:
        added.update(t["chars_added"])
    print(f"plan: {len(todo)} line(s) across {len({t['file'] for t in todo})} file(s), "
          f"{len({t['section'] for t in todo})} section(s)")
    print(f"  characters added: {dict(added)}  (total {sum(added.values())})")
    outside = {c: n for c, n in added.items() if c not in OPERATORS}
    print(f"  added characters outside the operator set: {outside or 'none'}")
    if refused:
        print(f"  refused {len(refused)} line(s):")
        for r in refused[:8]:
            print(f"    {r.get('section')}: {r['why'][:90]}")
    RECORD.write_text(json.dumps({"todo": todo, "refused": refused}, indent=1))
    print(f"  full record: {RECORD}")

    if not a.plan and not a.apply:
        print("\n(no writes: pass --plan to inspect, --apply to write)")
        return 0
    if a.plan or not todo or outside:
        if outside:
            print("\nREFUSING to apply: characters outside the operator set would be added")
        return 0 if not outside else 1

    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()
    tag = f"operator-apply-rollback-{head}"
    subprocess.run(["git", "tag", "-f", tag], cwd=ROOT, check=True)
    print(f"\nrollback point: tag {tag} (HEAD {head})")

    total = 0
    for i in range(0, len(todo), a.block_size):
        block = todo[i:i + a.block_size]
        res = apply_block(block)
        total += res["lines_written"]
        files = sorted({b["file"] for b in block})
        stat = subprocess.run(["git", "diff", "--numstat", "--"] + files, cwd=ROOT,
                              capture_output=True, text=True).stdout.strip().splitlines()
        expected = Counter(b["file"] for b in block)
        seen: dict[str, tuple[int, int]] = {}
        for line in stat:
            if not line.strip():
                continue
            ins, dele, path = line.split("\t")
            seen[path] = (int(ins), int(dele))
        ok = all(seen.get(f) == (expected[f], expected[f]) for f in expected)
        print(f"  block {i // a.block_size + 1}: {res['lines_written']} line(s) in "
              f"{res['files']} file(s); per-file +ins/-del "
              f"{ {k: v for k, v in seen.items()} } {'ok' if ok else 'MISMATCH'}")
    print(f"\napplied {total} line(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

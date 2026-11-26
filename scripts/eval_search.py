#!/usr/bin/env python3
"""Search relevance eval: hits@5/hits@10 on data/golden_eval.json against /api/search/hybrid."""
import json, urllib.request, urllib.parse

BASE = "http://localhost:8765/api/search/hybrid"
def norm(r):
    st = (r.get("source_type") or "").lower()
    act = (r.get("act") or "").lower()
    sec = (r.get("section") or "").lower()
    if st and act and sec:
        return f"{st}:{act}:{sec}"
    return ""

def run(q, limit=10):
    url = BASE + "?" + urllib.parse.urlencode({"q": q, "limit": limit})
    with urllib.request.urlopen(url, timeout=30) as fh:
        return json.load(fh)

def main():
    golden = json.load(open("data/golden_eval.json"))
    hit5 = hit10 = 0
    rows = []
    for g in golden:
        d = run(g["q"], 10)
        keys = [norm(r) for r in d.get("results", [])]
        h5 = any(e in keys[:5] for e in g["expect"])
        h10 = any(e in keys[:10] for e in g["expect"])
        hit5 += h5; hit10 += h10
        rows.append((g["q"], g["expect"], h5, h10, keys[:3]))
    print(f"\nhits@5: {hit5}/{len(golden)}  hits@10: {hit10}/{len(golden)}  P@5: {hit5/len(golden):.2f}")
    print(f"{'query':<45} {'h@5':<4} {'h@10':<5} top3")
    for q, ex, h5, h10, top3 in rows:
        print(f"{q[:44]:<45} {str(h5):<4} {str(h10):<5} {top3}")

if __name__ == "__main__":
    main()

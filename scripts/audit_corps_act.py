#!/usr/bin/env python3
"""Audit corporations act markdown quality."""

import os, re, json
from pathlib import Path

BASE = Path(f"{Path(__file__).resolve().parent.parent}/data/corporations-act-2001/sections")

samples = [
    ("part-1/division-1.1/1.md", "s.1 Short title"),
    ("part-1/division-1.2/9.md", "s.9 Dictionary"),
    ("part-1/division-1.2/50.md", "s.50 Related body corporate"),
    ("part-2B/division-2B.1/124.md", "s.124 Legal capacity"),
    ("part-2D/division-2D/198A.md", "s.198A Powers of directors"),
    ("part-2D/division-2D/198B.md", "s.198B Rights to inspect books"),
    ("part-2H/division-2H.1/254A.md", "s.254A Power to issue shares"),
    ("part-5/division-5.1/411.md", "s.411 Arrangements"),
    ("part-5B/division-5B.1/601BA.md", "s.601BA Registration as companies"),
    ("part-6/division-6.1/606.md", "s.606 Prohibition on acquisitions"),
    ("part-7/division-7.6/911A.md", "s.911A Licensing requirement"),
    ("part-7/division-7.1/761A.md", "s.761A Definitions"),
    ("part-7/division-7.11/1070A.md", "s.1070A Nature of shares"),
    ("part-8A/division-8A.1/1211.md", "s.1211 Object of Chapter"),
    ("part-8B/division-8B.1/1221.md", "s.1221 Overview"),
    ("part-9/division-9.4B/1317E.md", "s.1317E Declaration of contravention"),
    ("part-9/division-9.4AAA/1317AA.md", "s.1317AA Whistleblower protection"),
    ("part-10/division-10.1/1371.md", "s.1371 Definitions (transitional)"),
    ("part-sch4/division-sch4.2/1400.md", "s.1400 Definitions (sch4)"),
    ("part-2G/division-2G.2/249Y.md", "s.249Y Notice of meetings"),
]


class AuditIssue:
    def __init__(self, section, issue_type, detail):
        self.section = section
        self.issue_type = issue_type
        self.detail = detail
    def __str__(self):
        return f"[{self.issue_type}] {self.section}: {self.detail}"


issues = []
ok_count = 0

for rel_path, description in samples:
    p = BASE / rel_path
    if not p.exists():
        issues.append(AuditIssue(description, "MISSING", "File not found"))
        continue

    text = p.read_text(encoding="utf-8")
    lines = text.split("\n")

    # 1. Check frontmatter
    if not text.startswith("---"):
        issues.append(AuditIssue(description, "NO_FM", "No YAML frontmatter"))
        continue

    fm_end = text.find("---", 3)
    if fm_end < 0:
        issues.append(AuditIssue(description, "BAD_FM", "Frontmatter not closed"))
        continue

    fm_text = text[3:fm_end].strip()
    fm = {}
    for line in fm_text.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"')

    required_keys = ["act", "part", "section", "section_title"]
    for k in required_keys:
        if k not in fm:
            issues.append(AuditIssue(description, "MISSING_FM", f"Missing key: {k}"))

    body_start = fm_end + 4
    body = text[body_start:]

    # 2. Check heading
    heading_match = re.search(r"^# (.+)", body, re.MULTILINE)
    if not heading_match:
        issues.append(AuditIssue(description, "NO_HEADING", "Heading missing"))
    else:
        heading = heading_match.group(1).strip()
        expected = f"{fm.get('section','')} {fm.get('section_title','')}"
        if heading != expected:
            issues.append(AuditIssue(description, "HEADING_MISMATCH",
                f"Expected {expected!r}, Got {heading!r}"))

    # 3. No s. prefix in body
    body_text = body
    s_prefixes = re.findall(r"\bs\.\d+", body_text)
    if s_prefixes:
        issues.append(AuditIssue(description, "S_PREFIX", f"s. in body: {s_prefixes[:3]}"))

    # 4. No encoding artifacts
    bad_chars = set()
    for c in body:
        if ord(c) > 127 and ord(c) not in {
            0x2014, 0x2013, 0x2018, 0x2019, 0x201c, 0x201d,
            0x2022, 0x2026, 0x2122, 0x00b0, 0x00a7, 0x2011
        }:
            bad_chars.add(f"U+{ord(c):04X}")
    if bad_chars:
        issues.append(AuditIssue(description, "ENCODING", f"Bad chars: {bad_chars}"))

    # 5. Heading duplication in body
    if heading_match:
        h_words = heading.split(" ", 1)
        if len(h_words) > 1:
            after_h = body[heading_match.end():].strip()
            if after_h.startswith(h_words[1][:30]):
                issues.append(AuditIssue(description, "DUP_HEADING", "Title duplicated in body"))

    # 6. Paragraph structure stats
    body_after_h = body[heading_match.end():].strip() if heading_match else body.strip()
    para = re.findall(r"^\s*\([a-z]\)", body_after_h, re.MULTILINE)
    subpara = re.findall(r"^\s*\([ivxlcdm]+\)", body_after_h, re.MULTILINE)
    subsub = re.findall(r"^\s*\([A-Z]\)", body_after_h, re.MULTILINE)

    if not [i for i in issues if i.section == description]:
        ok_count += 1

    idx = samples.index((rel_path, description))
    if idx < 4 or idx % 5 == 0:
        print(f"""
--- {description} ---
Heading: {heading if heading_match else 'MISSING!'}
Body: {len(body)} chars
Paras(a): {len(para)}  Sub-paras(i): {len(subpara)}  Sub-sub(A): {len(subsub)}
Notes: {len(re.findall(r'Note', body_after_h))}
Encoding issues: {len(bad_chars)}
Start of body:
{body_after_h[:200]}
        """)

print(f"\n\n=== AUDIT SUMMARY ===")
print(f"Checked: {len(samples)}")
print(f"OK: {ok_count}")
print(f"Issues: {len(issues)}")
for i in issues:
    print(f"  {i}")
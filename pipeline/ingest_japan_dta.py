#!/usr/bin/env python3
"""Build data/treaties/japan/ from the ATO legal database (CCH) text of the
current Australia-Japan Convention (ITAA 1953 sched 6, agreement 1).

Source: https://www.ato.gov.au/law/view/print?DocID=RPC/19530082/Sch6-Agt1-ArtN
"""
import os
from curl_cffi import requests
import re, html as H, json
from pathlib import Path

SCH = 6
SLUG = "japan"
COUNTRY = "Japan"
BASE = "https://www.ato.gov.au/law/view/print?DocID=RPC/19530082/Sch6-Agt1-Art{art}&PiT=99991231235958"
MAX_ART = 32
OUT = Path(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "treaties")) / SLUG
ARTDIR = OUT / "articles"

LABEL_RE = re.compile(r'<strong>\s*(.*?)\s*</strong>', re.S)
CLAUSE_RE = re.compile(r'^\(([0-9]+|[a-zA-Z]{1,4}|[ivxlcdm]+)\)$')


def fetch(art):
    r = requests.get(BASE.format(art=art), impersonate="chrome", timeout=40)
    return r.text if r.status_code == 200 else None


def strip_tags(s):
    return re.sub(r'<[^>]+>', '', s)


def fix_quotes(t):
    out = []
    i, n, inq = 0, len(t), False
    while i < n:
        ch = t[i]
        if ch == '"':
            if not inq:
                while out and out[-1] == ' ':
                    out.pop()
                if out and (out[-1].isalnum() or out[-1] in ',;:.)'):
                    out.append(' ')
                out.append('"')
                inq = True
                i += 1
                while i < n and t[i] == ' ':
                    i += 1
                continue
            else:
                while out and out[-1] == ' ':
                    out.pop()
                out.append('"')
                inq = False
                i += 1
                j = i
                while j < n and t[j] == ' ':
                    j += 1
                if j < n and t[j].isalnum():
                    out.append(' ')
                i = j
                continue
        out.append(ch)
        i += 1
    return ''.join(out)


def norm(txt):
    txt = H.unescape(strip_tags(txt))
    txt = txt.replace('\xa0', ' ').replace('\u2011', '-')
    txt = re.sub(r'\s+', ' ', txt).strip()
    txt = fix_quotes(txt)
    txt = re.sub(r"(\w) '", r"\1'", txt)
    txt = re.sub(r"'\s+s\b", "'s", txt)
    txt = re.sub(r'\(\s+', '(', txt)
    txt = re.sub(r'\s+([,;.)])', r'\1', txt)
    txt = re.sub(r'\s+', ' ', txt).strip()
    return txt


def parse_article(html_text, art):
    start = html_text.find('name="RPC/19530082/Sch6-Agt1-Art%d"' % art)
    if start < 0:
        return None, None
    end = html_text.find('Disclaimer and notice of copyright', start)
    region = html_text[start:(end if end > 0 else len(html_text))]
    region = re.sub(r'<a\b[^>]*>\s*</a>', '', region)
    region = re.sub(r'<a\b[^>]*>', '', region)
    region = region.replace('<blockquote>', ' ').replace('</blockquote>', ' ')
    region = re.sub(r'<br\s*/?>', ' ', region)
    region = re.sub(r'</?p\s*/?>', ' ', region)
    region = re.sub(r'</?div[^>]*>', ' ', region)

    matches = list(LABEL_RE.finditer(region))
    # heading strong "ARTICLE N" then title strong
    title = None
    title_end = 0
    for idx, m in enumerate(matches):
        if re.match(r'^ARTICLE\s+%d$' % art, norm(m.group(1)), re.I):
            if idx + 1 < len(matches):
                title = norm(matches[idx + 1].group(1))
                title_end = matches[idx + 1].end()
            break
    if title is None:
        title = "Article %d" % art
    body_region = region[title_end:]
    for marker in ("IN WITNESS WHEREOF", "PROTOCOL At the signing", "PROTOCOL  At the signing"):
        c = body_region.find(marker)
        if c > 0:
            body_region = body_region[:c]
            break

    valid = [(m.start(), m.end(), norm(m.group(1)))
             for m in LABEL_RE.finditer(body_region)
             if CLAUSE_RE.match(norm(m.group(1)))]
    clauses = []  # (label or None, text)
    lead = body_region[:valid[0][0]] if valid else body_region
    if norm(lead):
        clauses.append((None, norm(lead)))
    for i, (s, e, lab) in enumerate(valid):
        tend = valid[i + 1][0] if i + 1 < len(valid) else len(body_region)
        txt = norm(body_region[e:tend])
        clauses.append((lab, txt))

    numbered = any(lab and lab[1:-1].isdigit() for lab, _ in clauses)
    out = []
    for lab, txt in clauses:
        if lab is None:
            if numbered:
                out.append(txt)
            else:
                out.append('**(1)** %s' % txt)
        elif lab[1:-1].isdigit():
            out.append('**(%s)** %s' % (lab[1:-1], txt))
        else:
            out.append('- **(%s)** %s' % (lab[1:-1], txt))
    return title, '\n\n'.join(out).strip()


def main():
    ARTDIR.mkdir(parents=True, exist_ok=True)
    articles = []
    for art in range(1, MAX_ART + 1):
        html_text = fetch(art)
        if html_text is None:
            print("STOP at", art)
            break
        title, body = parse_article(html_text, art)
        if not title or not body:
            print("PROBLEM art %d title=%r body=%r" % (art, title, bool(body)))
            continue
        slug_title = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
        art_slug = "article-%02d-%s" % (art, slug_title)
        fm = ('---\n'
              'country: "%s"\n'
              'country_slug: "%s"\n'
              'treaty_schedule: %d\n'
              'article: %d\n'
              'title: "Article %d \u2014 %s"\n'
              '---\n' % (COUNTRY, SLUG, SCH, art, art, title))
        (ARTDIR / (art_slug + '.md')).write_text(fm + body + '\n', encoding='utf-8')
        articles.append({
            "article": art,
            "title": "Article %d \u2014 %s" % (art, title),
            "slug": art_slug,
            "file": "articles/%s.md" % art_slug,
        })
        print("Art %2d: %-52s (%d chars)" % (art, title, len(body)))
    tree = {"treaty": COUNTRY, "country_slug": SLUG, "schedule": SCH,
            "articles": articles, "total": len(articles)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tree.json").write_text(json.dumps(tree, indent=2), encoding='utf-8')
    print("total:", len(articles))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Ingest Australian Double Tax Agreements into legislation-explorer format.

Fetches DTA texts from the ATO legal database (works), splits into per-article
markdown files with YAML frontmatter, and generates tree.json per country.

Usage:
    python3 pipeline/ingest_dta.py                    # batch all
    python3 pipeline/ingest_dta.py --sch=5             # single: schedule 5 (Singapore)
    python3 pipeline/ingest_dta.py --force-rebuild     # re-fetch all
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from html.parser import HTMLParser

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "treaties"
ATO_BASE = "https://www.ato.gov.au/law/view/print?DocID=RPC/19530082/Sch{sch}-Agt0-Art{art}&PiT=99991231235958"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

# Standard OECD Model Tax Convention article titles
OECD_TITLES = {
    1: "Personal Scope", 2: "Taxes Covered", 3: "General Definitions",
    4: "Resident", 5: "Permanent Establishment", 6: "Income from Real Property",
    7: "Business Profits", 8: "Shipping and Air Transport", 9: "Associated Enterprises",
    10: "Dividends", 11: "Interest", 12: "Royalties", 13: "Capital Gains",
    14: "Independent Personal Services", 15: "Income from Employment",
    16: "Directors' Fees", 17: "Entertainers and Sportspersons", 18: "Pensions",
    19: "Government Service", 20: "Students", 21: "Other Income",
    22: "Elimination of Double Taxation", 23: "Mutual Agreement Procedure",
    24: "Exchange of Information", 25: "Assistance in Collection",
    26: "Members of Diplomatic Missions", 27: "Territorial Extension",
    28: "Entry into Force", 29: "Termination", 30: "Miscellaneous",
}

# Map: schedule -> (country_slug, country_name, max_articles, [title_overrides])
DTA_SCHEDULES = {
    1:  ("taipei", "Taipei (ACIO-TECO)", 26, {26: "Entry into Force"}),
    2:  ("usa", "United States of America", 27, {26: "Entry into Force", 27: "Termination"}),
    3:  ("canada", "Canada", 27),
    4:  ("new-zealand", "New Zealand", 27),
    5:  ("singapore", "Singapore", 22, {22: "Entry into Force"}),
    10: ("netherlands", "Netherlands", 30),
    11: ("france", "France", 28),
    13: ("belgium", "Belgium", 29),
    14: ("philippines", "Philippines", 28),
    16: ("malaysia", "Malaysia", 25),
    17: ("sweden", "Sweden", 27),
    18: ("denmark", "Denmark", 27),
    20: ("ireland", "Ireland", 27),
    21: ("italy", "Italy", 29),
    22: ("korea", "Korea", 27),
    23: ("norway", "Norway", 27),
    24: ("malta", "Malta", 28),
    25: ("finland", "Finland", 29),
    27: ("austria", "Austria", 27),
    28: ("china", "China", 27),
    29: ("papua-new-guinea", "Papua New Guinea", 27),
    30: ("thailand", "Thailand", 27),
    31: ("sri-lanka", "Sri Lanka", 27),
    32: ("fiji", "Fiji", 27),
    33: ("hungary", "Hungary", 27),
    34: ("kiribati", "Kiribati", 27),
    35: ("india", "India", 29),
    36: ("poland", "Poland", 27),
    37: ("indonesia", "Indonesia", 28),
    38: ("vietnam", "Vietnam", 28),
    39: ("spain", "Spain", 27),
    40: ("czech-republic", "Czech Republic", 27),
    42: ("south-africa", "South Africa", 27),
    43: ("slovakia", "Slovakia", 28),
    44: ("argentina", "Argentina", 28),
    45: ("romania", "Romania", 28),
    46: ("russia", "Russia", 27),
    47: ("mexico", "Mexico", 28),
    51: ("turkey", "Turkey", 27),
    53: ("chile", "Chile", 28),
    62: ("israel", "Israel", 28),
    63: ("iceland", "Iceland", 27),
}

AIRLINE_ONLY = {8, 12, 26}


class TextExtractor(HTMLParser):
    """Extract clean text from ATO legal database HTML, stripping boilerplate."""
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip_tags = {'script', 'style', 'noscript'}
        self.skip = False
        self.skip_depth = 0
        self.in_lawbody = False
        self.lawbody_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.skip_tags:
            self.skip = True
            self.skip_depth = 1
            return
        attrs_dict = dict(attrs)
        if tag == 'div' and attrs_dict.get('id') == 'lawBody':
            self.in_lawbody = True
            self.lawbody_depth = 1

    def handle_endtag(self, tag):
        if self.skip:
            return

    def handle_data(self, data):
        if not self.in_lawbody:
            return
        text = data.strip()
        if not text:
            return
        if text in ('View history note', 'Hide history note', 'History', 'View history reference'):
            return
        text = text.replace('&#45;', '-').replace('&nbsp;', ' ').replace('&amp;', '&')
        self.parts.append(text + '\n')

    def handle_entityref(self, name):
        if not self.in_lawbody:
            return
        if name == 'nbsp':
            self.parts.append(' ')

    def get_text(self):
        text = ''.join(self.parts)
        text = re.sub(r'\n{3,}', '\n\n', text)
        disclaimer_idx = text.find('Disclaimer and notice')
        if disclaimer_idx > -1:
            text = text[:disclaimer_idx].strip()
        copy_idx = text.find('Copyright notice')
        if copy_idx > -1:
            text = text[:copy_idx].strip()
        return text.strip()


def fetch_article(sch: int, art: int) -> str | None:
    url = ATO_BASE.format(sch=sch, art=art)
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        html = resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"  ERROR: HTTP {e.code} for Sch{sch} Art{art}")
        return None
    except Exception as e:
        print(f"  ERROR: {e} for Sch{sch} Art{art}")
        return None
    extractor = TextExtractor()
    extractor.feed(html)
    text = extractor.get_text()
    if 'Page not found' in text[:200]:
        return None
    return text


def get_article_title(art_num: int, sch: int) -> str:
    info = DTA_SCHEDULES.get(sch)
    overrides = info[3] if info and len(info) > 3 else {}
    if art_num in overrides:
        return overrides[art_num]
    return OECD_TITLES.get(art_num, f"Article {art_num}")


def slugify(text: str) -> str:
    text = text.lower().replace(' ', '-')
    return re.sub(r'[^a-z0-9-]', '', text)


def ingest_country(sch: int, force: bool = False) -> bool:
    info = DTA_SCHEDULES.get(sch)
    if not info:
        return False
    slug, name, max_arts = info[0], info[1], info[2]
    country_dir = DATA_DIR / slug
    articles_dir = country_dir / "articles"
    tree_path = country_dir / "tree.json"
    if not force and tree_path.exists():
        print(f"SKIP {name}: already ingested")
        return False
    country_dir.mkdir(parents=True, exist_ok=True)
    articles_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*60}")
    print(f"Ingesting {name} (Sch{sch}, up to {max_arts} articles)...")
    print(f"{'='*60}")
    articles = []
    for art in range(1, max_arts + 1):
        time.sleep(0.25)
        text = fetch_article(sch, art)
        if text is None:
            print(f"  Art {art:2d}: not found (stopping at {art-1})")
            break
        title = get_article_title(art, sch)
        # Remove leading article number / heading artifacts
        lines = text.split('\n')
        cleaned_lines = []
        for line in lines:
            stripped = line.strip()
            # Skip pure "ARTICLE N" heading lines
            if re.match(r'^ARTICLE\s+\d+[A-Z]*\s*$', stripped):
                continue
            cleaned_lines.append(line)
        body = '\n'.join(cleaned_lines).strip()
        art_slug = f"article-{art:02d}-{slugify(title)}"
        art_path = articles_dir / f"{art_slug}.md"
        fm = f"""---
country: "{name}"
country_slug: "{slug}"
treaty_schedule: {sch}
article: {art}
title: "Article {art} \u2014 {title}"
---
{body}
"""
        art_path.write_text(fm, encoding='utf-8')
        articles.append({
            "article": art,
            "title": f"Article {art} \u2014 {title}",
            "slug": art_slug,
            "file": f"articles/{art_slug}.md",
        })
        print(f"  Art {art:2d}: {title} \u2713 ({len(body)} chars)")
    tree = {"treaty": name, "country_slug": slug, "schedule": sch,
            "articles": articles, "total": len(articles)}
    tree_path.write_text(json.dumps(tree, indent=2), encoding='utf-8')
    print(f"  \u2713 {name}: {len(articles)} articles ingested")
    return True


def main():
    force = '--force-rebuild' in sys.argv
    single_sch = None
    for arg in sys.argv[1:]:
        if arg.startswith('--sch='):
            single_sch = int(arg.split('=')[1])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if single_sch:
        ingest_country(single_sch, force)
        return
    schs = sorted(DTA_SCHEDULES.keys())
    ok = skip = fail = 0
    for sch in schs:
        if sch in AIRLINE_ONLY:
            continue
        try:
            if ingest_country(sch, force):
                ok += 1
            else:
                skip += 1
        except Exception as e:
            print(f"ERROR Sch{sch}: {e}")
            fail += 1
    print(f"\n{'='*60}")
    print(f"Done: {ok} ingested, {skip} skipped, {fail} failed")


if __name__ == '__main__':
    main()

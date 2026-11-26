"""SQLite FTS5 search service."""
from __future__ import annotations
import json
import logging
import os
import re
import sqlite3
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from backend.config import DATA_DIR, RULING_DIR, SEARCH_DB, TREATIES_DIR
from backend.doc_ids import court_name_from_citation
from backend.services.data_loader import load_tree, get_act_section_content, validate_ruling_meta

from backend.services.searchd_client import normalize_series, ruling_series

logger = logging.getLogger(__name__)


@contextmanager
def search_conn():
    """Yield a fresh SQLite connection (per-request safety)."""
    conn = sqlite3.connect(str(SEARCH_DB))
    conn.row_factory = sqlite3.Row
    # the exact ruling-series rule shared with searchd, usable in SQL
    conn.create_function("ruling_series", 1, ruling_series, deterministic=True)
    try:
        yield conn
    finally:
        conn.close()


_WITHDRAWN_PATTERNS = [
    r'\b(withdrawn|Archived|superseded|no longer current)\b',
    r'has been replaced by',
]


def _check_withdrawn(content: str) -> bool:
    """Mirror data_loader._check_withdrawn — scan the first 2000 chars only."""
    head = content[:2000]
    return any(re.search(p, head, re.IGNORECASE) for p in _WITHDRAWN_PATTERNS)


def init_search_index() -> None:
    """Build or rebuild the FTS5 search index from sections, rulings, and cases.
    
    Uses WAL mode and explicit transactions so partial failures roll back
    cleanly instead of leaving a 0-byte/empty DB.
    """
    with search_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN")
        try:
            # --- Sections FTS ---
            conn.execute("DROP TABLE IF EXISTS sections_fts")
            conn.execute("""
                CREATE VIRTUAL TABLE sections_fts USING fts5(
                    act, section, title, content,
                    tokenize='porter'
                )
            """)
            conn.execute("DROP TABLE IF EXISTS sections_meta")
            conn.execute("""
                CREATE TABLE sections_meta (
                    act TEXT, section TEXT, title TEXT, part TEXT, division TEXT,
                    UNIQUE (act, section)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meta_act_section ON sections_meta(act, section)")

            # Track (act, section_id) pairs already indexed so each section is
            # inserted exactly once (first occurrence wins for part/division
            # metadata).  Some tree.json files list the same section under
            # multiple parts/divisions, which previously produced duplicate rows
            # in both sections_fts and sections_meta and multiplied search hits.
            seen: set[tuple[str, str]] = set()

            for act_dir in DATA_DIR.iterdir():
                if not act_dir.is_dir() or not (act_dir / "tree.json").exists():
                    continue
                act = act_dir.name
                tree = load_tree(act)
                for part in tree.get("parts", []):
                    part_id = part.get("id", "")
                    for sec in part.get("sections", []):
                        _index_section(conn, act, sec, part_id, "", seen)
                    for div in part.get("divisions", []):
                        div_id = div.get("id", "")
                        for sec in div.get("sections", []):
                            _index_section(conn, act, sec, part_id, div_id, seen)
                        for sub in div.get("subdivisions", []):
                            for sec in sub.get("sections", []):
                                _index_section(conn, act, sec, part_id, div_id, seen)

            # --- Rulings FTS ---
            conn.execute("DROP TABLE IF EXISTS rulings_fts")
            conn.execute("""
                CREATE VIRTUAL TABLE rulings_fts USING fts5(
                    citation, title, content,
                    tokenize='porter'
                )
            """)
            conn.execute("DROP TABLE IF EXISTS rulings_meta")
            conn.execute("""
                CREATE TABLE rulings_meta (
                    citation TEXT UNIQUE, title TEXT, year INTEGER, ruling_type TEXT,
                    withdrawn INTEGER DEFAULT 0
                )
            """)

            ruled_seen: set[str] = set()
            for f in sorted(RULING_DIR.glob("*.txt")):
                if f.name.endswith(".meta.json") or f.name.startswith("."):
                    continue
                citation = f.stem
                if citation in ruled_seen:
                    continue
                ruled_seen.add(citation)

                title = citation
                year = 0
                ruling_type = ""
                m = re.match(r'^([A-Za-z]+)_(\d{2,4})_(\d+)', f.stem)
                if not m:
                    # Draft citations: TD_2026_D1 → type TD, year 2026
                    m = re.match(r'^([A-Za-z]+)_(\d{2,4})_D(\d+)', f.stem)
                if m:
                    ruling_type = m.group(1).upper()
                    year = int(m.group(2))
                    if year < 100:
                        year += 1900 if year >= 90 else 2000

                meta_path = f.with_suffix(f.suffix + ".meta.json")
                if not meta_path.exists():
                    meta_path = f.parent / (f.stem + ".txt.meta.json")
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    # Completeness gate: fail the index build if a fetched
                    # ruling's citation/status/type/year disagrees with its file.
                    validate_ruling_meta(f.stem, meta)
                    title = meta.get("title", citation)

                # Fallback: if title is still the citation (no meta), try summary file
                if title == citation:
                    summary_dir = RULING_DIR / "summaries"
                    summ_path = summary_dir / f"{citation}.json"
                    if summ_path.exists():
                        try:
                            summ_data = json.loads(summ_path.read_text(encoding="utf-8"))
                            title = summ_data.get("title", summ_data.get("subject", title))
                        except Exception:
                            pass

                content_raw = f.read_text(encoding="utf-8", errors="replace")
                content = re.sub(r'[#*`_\[\]\(\)]', ' ', content_raw)
                content = re.sub(r'\s+', ' ', content).strip()[:50000]

                conn.execute(
                    "INSERT INTO rulings_fts (citation, title, content) VALUES (?, ?, ?)",
                    (citation, title, content)
                )
                conn.execute(
                    "INSERT INTO rulings_meta (citation, title, year, ruling_type, withdrawn) VALUES (?, ?, ?, ?, ?)",
                    (citation, title, year, ruling_type, 1 if _check_withdrawn(content) else 0)
                )

            conn.commit()

            # --- Private rulings FTS (57k rulings, term search over body) ---
            conn.execute("DROP TABLE IF EXISTS private_rulings_fts")
            conn.execute(
                "CREATE VIRTUAL TABLE private_rulings_fts USING fts5("
                "authnum, name, content, tokenize='porter')"
            )
            conn.execute("DROP TABLE IF EXISTS private_rulings_meta")
            conn.execute(
                "CREATE TABLE private_rulings_meta ("
                "authnum TEXT UNIQUE, name TEXT, year INTEGER, date_of_advice TEXT)"
            )

            pr_index_path = DATA_DIR / "private_rulings_index.json"
            pr_json_dir = Path.home() / ".hermes" / "private_rulings" / "data" / "json"
            pr_count = 0
            try:
                pr_index = json.loads(pr_index_path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("Private rulings index load failed")
                pr_index = {}
            for authnum, meta in pr_index.items():
                name = meta.get("name") or authnum
                year = meta.get("year")
                content_parts = [name]
                # Body text from the per-ruling JSON when available
                if pr_json_dir.exists():
                    jp = pr_json_dir / f"{authnum}.json"
                    if jp.exists():
                        try:
                            jd = json.loads(jp.read_text(encoding="utf-8"))
                            ft = jd.get("formatted_text") or ""
                            if ft:
                                content_parts.append(ft)
                            else:
                                for k in ("subject", "facts", "reasons_for_decision", "relevant_legislation", "qa_pairs"):
                                    v = jd.get(k)
                                    if isinstance(v, list):
                                        content_parts.extend(str(x) for x in v if x)
                                    elif v:
                                        content_parts.append(str(v))
                        except Exception:
                            pass
                content = re.sub(r'[#*`_\[\]\(\)]', ' ', " ".join(content_parts))
                content = re.sub(r'\s+', ' ', content).strip()[:16000]
                if not content.strip():
                    continue
                conn.execute(
                    "INSERT INTO private_rulings_fts (authnum, name, content) VALUES (?, ?, ?)",
                    (authnum, name, content)
                )
                conn.execute(
                    "INSERT INTO private_rulings_meta (authnum, name, year, date_of_advice) VALUES (?, ?, ?, ?)",
                    (authnum, name, year, meta.get("date_of_advice"))
                )
                pr_count += 1
            logger.info(f"Private rulings FTS indexed: {pr_count}")

            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("FTS index build failed, rolled back")
            raise

    # --- Tax Treaties FTS ---
    if TREATIES_DIR.exists():
        try:
            with search_conn() as conn:
                conn.execute("DROP TABLE IF EXISTS treaties_fts")
                conn.execute("""
                    CREATE VIRTUAL TABLE treaties_fts USING fts5(
                        country, article, title, content,
                        tokenize='porter'
                    )
                """)
                conn.execute("DROP TABLE IF EXISTS treaties_meta")
                conn.execute("""
                    CREATE TABLE treaties_meta (
                        country TEXT, article INTEGER,
                        country_slug TEXT, title TEXT, slug TEXT,
                        UNIQUE (country_slug, article)
                    )
                """)
                conn.execute("BEGIN")
                indexed_count = 0
                for country_dir in sorted(TREATIES_DIR.iterdir()):
                    if not country_dir.is_dir():
                        continue
                    tree_path = country_dir / "tree.json"
                    if not tree_path.exists():
                        continue
                    try:
                        tree = json.loads(tree_path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    country_slug = country_dir.name
                    country_name = tree.get("treaty", country_slug)
                    for art in tree.get("articles", []):
                        art_file = country_dir / art["file"]
                        if not art_file.exists():
                            continue
                        raw = art_file.read_text(encoding="utf-8", errors="replace")
                        # Strip YAML frontmatter
                        if raw.startswith("---"):
                            parts = raw.split("---", 2)
                            if len(parts) >= 3:
                                raw = parts[2].lstrip("\n")
                        content = re.sub(r'[#*`_\[\]\(\)]', ' ', raw)
                        content = re.sub(r'\s+', ' ', content).strip()[:50000]
                        unique_art_id = hash(f"{country_slug}/{art['article']}")
                        conn.execute(
                            "INSERT INTO treaties_fts (country, article, title, content) VALUES (?, ?, ?, ?)",
                            (country_name, str(art["article"]), art["title"], content)
                        )
                        conn.execute(
                            "INSERT INTO treaties_meta (country, article, country_slug, title, slug) VALUES (?, ?, ?, ?, ?)",
                            (country_name, art["article"], country_slug, art["title"], art["slug"])
                        )
                        indexed_count += 1
                conn.commit()
                logger.info(f"Treaties FTS indexed: {indexed_count} articles across {len([d for d in TREATIES_DIR.iterdir() if d.is_dir()])} countries")
        except Exception:
            logger.exception("Treaties FTS index failed (non-fatal)")

    # --- Case summaries FTS ---
    SUMMARIES_DIR = DATA_DIR / ".." / "scripts" / "cleaned" / "summaries"
    if SUMMARIES_DIR.exists():
        try:
            with search_conn() as conn:
                conn.execute("DROP TABLE IF EXISTS case_summaries_fts")
                conn.execute("""
                    CREATE VIRTUAL TABLE case_summaries_fts USING fts5(
                        citation, case_name, court, text,
                        tokenize='porter'
                    )
                """)
                conn.execute("BEGIN")
                count = 0
                for f in sorted(os.listdir(str(SUMMARIES_DIR))):
                    if not f.endswith(".json"):
                        continue
                    try:
                        with open(SUMMARIES_DIR / f) as fh:
                            s = json.load(fh)
                    except Exception:
                        continue
                    citation = s.get("citation", "")
                    case_name = s.get("case_name", "") or s.get("title", "")
                    court = s.get("court", "")
                    text_parts = [
                        s.get("facts", ""),
                        s.get("held", ""),
                        s.get("reasoning", ""),
                        s.get("outcome", ""),
                    ]
                    for lst_key in ("issues", "cases_cited", "legislation_cited"):
                        val = s.get(lst_key, [])
                        if isinstance(val, list):
                            text_parts.extend(str(item) for item in val if isinstance(item, str))
                        elif isinstance(val, str):
                            text_parts.append(val)
                    text = re.sub(r'\s+', ' ', " ".join(text_parts)).strip()
                    if text:
                        conn.execute(
                            "INSERT INTO case_summaries_fts (citation, case_name, court, text) VALUES (?, ?, ?, ?)",
                            (citation, case_name, court, text)
                        )
                        count += 1
                conn.commit()
                logger.info(f"Case summaries FTS indexed: {count} cases")
        except Exception:
            logger.exception("Case summaries FTS index failed (non-fatal)")

    # --- Full case-list index (FCA/FCAFC/HCA) ---
    # Every judgment in the three authoritative lists — searchable by medium-
    # neutral citation, reported/parallel citation, case name, catchwords, and
    # subject.  Non-fatal: search degrades to the summary-only index if it fails.
    try:
        with search_conn() as conn:
            build_case_index(conn)
    except Exception:
        logger.exception("Case index FTS build failed (non-fatal)")

    # --- Regulatory guides FTS (ASIC RGs) ---
    try:
        with search_conn() as conn:
            build_rg_fts(conn)
    except Exception:
        logger.exception("Regulatory guides FTS index failed (non-fatal)")

    # --- AFSA practice guidance FTS ---
    try:
        with search_conn() as conn:
            build_afsa_fts(conn)
    except Exception:
        logger.exception("AFSA practice guidance FTS index failed (non-fatal)")

    logger.info(f"Search index built: {SEARCH_DB}")


def build_rg_fts(conn: sqlite3.Connection) -> int:
    """(Re)build regulatory_guides_fts from the RG texts. Returns row count.

    Standalone so the RG table can be rebuilt without a full index rebuild.
    """
    rg_dir = DATA_DIR / "regulatory-guides"
    rg_texts_dir = rg_dir / "texts"
    rg_summaries_dir = rg_dir / "summaries"
    manifest_titles: dict[str, str] = {}
    try:
        for m in json.loads((rg_dir / "rg_manifest.json").read_text(encoding="utf-8")):
            manifest_titles[str(m["rg_number"])] = m.get("title") or ""
    except Exception:
        pass
    conn.execute("DROP TABLE IF EXISTS regulatory_guides_fts")
    conn.execute("""
        CREATE VIRTUAL TABLE regulatory_guides_fts USING fts5(
            rg, title, content, tokenize='porter'
        )
    """)
    conn.execute("BEGIN")
    rg_count = 0
    if rg_texts_dir.exists():
        for f in sorted(rg_texts_dir.glob("RG_*.txt")):
            try:
                rg_id = str(int(f.stem.replace("RG_", "")))
            except ValueError:
                rg_id = f.stem.replace("RG_", "")
            # Prefer the fresh manifest title; fall back to the summary JSON.
            title = manifest_titles.get(rg_id) or ""
            summ_path = rg_summaries_dir / f"{f.stem}.json"
            if not title and summ_path.exists():
                try:
                    meta = json.loads(summ_path.read_text(encoding="utf-8"))
                    title = meta.get("title") or ""
                except Exception:
                    pass
            title = title or f"RG {rg_id}"
            content = re.sub(r'[#*`_\[\]()]', ' ', f.read_text(encoding="utf-8", errors="replace"))
            content = re.sub(r'\s+', ' ', content).strip()[:50000]
            conn.execute(
                "INSERT INTO regulatory_guides_fts (rg, title, content) VALUES (?, ?, ?)",
                (rg_id, title, content)
            )
            rg_count += 1
    conn.commit()
    logger.info(f"Regulatory guides FTS indexed: {rg_count} RGs")
    return rg_count


def build_afsa_fts(conn: sqlite3.Connection) -> int:
    """(Re)build afsa_guides_fts from the AFSA practice-guidance texts."""
    afsa_dir = DATA_DIR / "afsa"
    afsa_texts_dir = afsa_dir / "texts"
    manifest_titles: dict[str, str] = {}
    try:
        for m in json.loads((afsa_dir / "afsa_manifest.json").read_text(encoding="utf-8")):
            manifest_titles[str(m["slug"])] = m.get("title") or ""
    except Exception:
        pass
    conn.execute("DROP TABLE IF EXISTS afsa_guides_fts")
    conn.execute("""
        CREATE VIRTUAL TABLE afsa_guides_fts USING fts5(
            slug, title, content, tokenize='porter'
        )
    """)
    conn.execute("BEGIN")
    afsa_count = 0
    if afsa_texts_dir.exists():
        for f in sorted(afsa_texts_dir.glob("*.txt")):
            slug = f.stem
            title = manifest_titles.get(slug) or slug
            content = re.sub(r'[#*`_\[\]()]', ' ', f.read_text(encoding="utf-8", errors="replace"))
            content = re.sub(r'\s+', ' ', content).strip()[:50000]
            conn.execute(
                "INSERT INTO afsa_guides_fts (slug, title, content) VALUES (?, ?, ?)",
                (slug, title, content)
            )
            afsa_count += 1
    conn.commit()
    logger.info(f"AFSA practice guidance FTS indexed: {afsa_count} docs")
    return afsa_count


# --- Full case-list index (FCA/FCAFC/HCA) -----------------------------------
# The three authoritative judgment lists (data/{fca,fcafc,hca}_all_cases.json)
# carry medium-neutral citation, parallel/reported citations, case name,
# catchwords and a derived subject.  Two tables:
#   case_index_fts  — FTS5 over all searchable text (citation + parallels,
#                     case name, subject, catchwords)
#   case_index_meta — one row per citation for exact lookup and result fields
#                     (court/year/npa not in the FTS columns).
CASE_INDEX_FILES = {
    "FCA": "fca_all_cases.json",
    "FCAFC": "fcafc_all_cases.json",
    "HCA": "hca_all_cases.json",
}

_CASE_FULL_RE = re.compile(r'^\[\s*(\d{4})\s*\]\s*(FCAFC|FCA|HCA)\s+(\d+)\s*$', re.IGNORECASE)
_CASE_BRACKETLESS_RE = re.compile(r'^(\d{4})\s+(FCAFC|FCA|HCA)\s+(\d+)\s*$', re.IGNORECASE)
_REPORTED_RE = re.compile(r'^(\(\d{4}\)\s+)?\d+\s+[A-Z][A-Za-z&.\' ]*\s+\d+$')


def build_case_index(conn: sqlite3.Connection) -> int:
    """(Re)build case_index_fts + case_index_meta from the full case lists.

    Standalone so the case index can be rebuilt without a full index rebuild.
    Returns the number of cases indexed.
    """
    conn.execute("DROP TABLE IF EXISTS case_index_fts")
    conn.execute("""
        CREATE VIRTUAL TABLE case_index_fts USING fts5(
            citation, all_citations, case_name, subject, catchwords,
            tokenize='porter'
        )
    """)
    conn.execute("DROP TABLE IF EXISTS case_index_meta")
    conn.execute("""
        CREATE TABLE case_index_meta (
            citation TEXT PRIMARY KEY,
            case_name TEXT,
            court TEXT,
            year INTEGER,
            subject TEXT,
            npa TEXT,
            catchwords TEXT,
            parallel_citations TEXT
        )
    """)
    conn.execute("BEGIN")
    count = 0
    for court, fn in CASE_INDEX_FILES.items():
        path = DATA_DIR / fn
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception(f"Case index: failed to load {fn}")
            continue
        for e in data:
            citation = (e.get("citation") or "").strip()
            if not citation:
                continue
            case_name = (e.get("case_name") or "").strip()
            subject = (e.get("subject") or "").strip()
            npa = (e.get("npa") or "").strip()
            catchwords = (e.get("catchwords") or "").strip()
            year = e.get("year")
            if not isinstance(year, int):
                try:
                    year = int(year or 0)
                except (TypeError, ValueError):
                    year = 0
            parallel = e.get("parallel_citations") or []
            if isinstance(parallel, str):
                parallel = [parallel]
            parallel = [str(p).strip() for p in parallel if str(p).strip()]
            all_citations = " ".join([citation] + parallel)
            conn.execute(
                "INSERT INTO case_index_fts (citation, all_citations, case_name, subject, catchwords) "
                "VALUES (?, ?, ?, ?, ?)",
                (citation, all_citations, case_name, subject, catchwords),
            )
            conn.execute(
                "INSERT INTO case_index_meta (citation, case_name, court, year, subject, npa, catchwords, parallel_citations) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (citation, case_name, court, year, subject, npa, catchwords, json.dumps(parallel)),
            )
            count += 1
    conn.commit()
    logger.info(f"Case index FTS indexed: {count} cases")
    return count


def _exact_case_citation(q: str) -> str | None:
    """Normalise a medium-neutral citation query to canonical '[YYYY] COURT N'."""
    m = _CASE_FULL_RE.match(q) or _CASE_BRACKETLESS_RE.match(q)
    if m:
        return f"[{m.group(1)}] {m.group(2).upper()} {m.group(3)}"
    return None


def _looks_reported(q: str) -> bool:
    """True if the query looks like a reported citation (e.g. '85 FCR 1')."""
    return bool(_REPORTED_RE.match(q))


def _case_index_row(row: sqlite3.Row, snippet: str | None = None) -> dict:
    cn = row["case_name"] or ""
    return {
        "citation": row["citation"],
        "case_name": cn,
        "court": court_name_from_citation(row["citation"]) or row["court"] or "",
        "year": row["year"] or "",
        "subject": row["subject"] or "",
        "npa": row["npa"] or "",
        "snippet": snippet or cn,
    }


def search_case_index(q: str, limit: int = 20, operator: str = "AND") -> list[dict]:
    """Search the full case list by citation, parallel citation, name or subject.

    Order of precedence: exact medium-neutral citation (pinned first), reported
    citation (LIKE on parallel_citations), then FTS5 over all text.  Dedupes by
    citation, capped at `limit`.
    """
    q = (q or "").strip()
    if not q:
        return []

    results: list[dict] = []
    seen: set[str] = set()

    def _add(row, snippet=None):
        r = _case_index_row(row, snippet)
        if r["citation"] not in seen:
            seen.add(r["citation"])
            results.append(r)

    with search_conn() as conn:
        # 1. Exact medium-neutral citation
        exact = _exact_case_citation(q)
        if exact:
            row = conn.execute(
                "SELECT citation, case_name, court, year, subject, npa "
                "FROM case_index_meta WHERE citation = ?",
                (exact,),
            ).fetchone()
            if row:
                _add(row)

        # 2. Reported citation — LIKE over the parallel-citations JSON array
        if _looks_reported(q):
            rows = conn.execute(
                "SELECT citation, case_name, court, year, subject, npa "
                "FROM case_index_meta WHERE parallel_citations LIKE ? "
                "ORDER BY year DESC LIMIT ?",
                (f"%{q}%", limit),
            ).fetchall()
            for row in rows:
                _add(row)

        # 3. FTS5 over citation + parallels + name + subject + catchwords
        tokens = q.split()
        quoted = []
        for tok in tokens:
            if tok.endswith('*') and len(tok) > 1:
                quoted.append(f'"{tok[:-1].replace(chr(34), chr(34) * 2)}"*')
            else:
                quoted.append('"' + tok.replace('"', '""') + '"')
        q_clean = (' OR ' if operator == "OR" else ' ').join(quoted)
        try:
            rows = conn.execute(
                "SELECT case_index_fts.citation, case_index_fts.case_name, "
                "case_index_meta.court, case_index_meta.year, case_index_meta.subject, "
                "case_index_meta.npa "
                "FROM case_index_fts "
                "JOIN case_index_meta ON case_index_fts.citation = case_index_meta.citation "
                "WHERE case_index_fts MATCH ? ORDER BY rank LIMIT ?",
                (q_clean, limit),
            ).fetchall()
            for row in rows:
                _add(row)
        except Exception:
            logger.exception("Case index FTS search failed")

    return results[:limit]


def _index_section(
    conn: sqlite3.Connection,
    act: str,
    sec: dict,
    part: str,
    division: str,
    seen: set[tuple[str, str]],
) -> None:
    sec_id = sec["id"]
    key = (act, sec_id)
    if key in seen:
        return
    seen.add(key)

    title = sec.get("title", "")
    try:
        fm, content_body = get_act_section_content(act, sec_id)
    except Exception:
        logger.exception(f"Error getting section content for {act}/{sec_id}")
        content_body = ""

    content = re.sub(r'<[^>]+>', ' ', content_body)
    for ch in '#*`_[]':
        content = content.replace(ch, ' ')
    content = ' '.join(content.split())[:50000]

    conn.execute(
        "INSERT INTO sections_fts (act, section, title, content) VALUES (?, ?, ?, ?)",
        (act, sec_id, title, content)
    )
    conn.execute(
        "INSERT INTO sections_meta (act, section, title, part, division) VALUES (?, ?, ?, ?, ?)",
        (act, sec_id, title, part, division)
    )


# Tax-terminology synonyms. Applied to unquoted query text only, whole words only.
SYNONYMS = {
    "main residence": ["principal place of residence", "PPOR", "family home"],
    "cgt": ["capital gains tax", "capital gain"],
    "rollover": ["roll-over", "roll over"],
    # WA prints "pay-roll tax" (Pay-roll Tax Assessment Act 2002); QLD/TAS/SA print "payroll"
    "payroll": ["pay-roll"],
    "pay-roll": ["payroll"],
    "personal services income": ["PSI"],
    "psi": ["personal services income"],
    "input tax credit": ["ITC"],
    "imputation": ["franking", "dividend imputation"],
    "small business": ["SBE"],
    "employee share scheme": ["ESS"],
    "deceased estate": ["estate of a deceased person"],
    "trading stock": ["stock in trade", "inventory"],
    "superannuation": ["super", "SMSF"],
    "margin scheme": ["GST margin"],
    "non-commercial loss": ["hobby loss"],
    "foreign resident": ["non-resident", "nonresident"],
    "staking": ["staking rewards", "rewards for holding tokens", "holding tokens"],
    # Corporate/ASIC acronyms (corporate-asic scope)
    "ncp": ["non-cash payment", "non cash payment"],
    "afsl": ["australian financial services licence", "financial services licence"],
    "financial services licence": ["afsl", "australian financial services licence"],
}
# Longest keys first so "personal services income" wins over a shorter overlap.
_SYNONYM_RE = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in sorted(SYNONYMS, key=len, reverse=True)) + r')\b',
    re.IGNORECASE,
)


def _fts_phrase(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _expand_query(q: str, operator: str = "AND") -> str:
    """Build the FTS5 MATCH string: quoted phrases pass through untouched,
    recognised terms in the free text become OR-groups with their synonyms.
    operator='OR' joins top-level terms with OR instead of AND."""
    out: list[str] = []
    for phrase, word in re.findall(r'"([^"]*)"|(\S+)', q):
        if phrase:
            out.append(_fts_phrase(phrase))
        elif word.endswith('*') and len(word) > 1:
            out.append(_fts_phrase(word[:-1]) + '*')
        else:
            out.append(word)
    # Synonym expansion runs over the free-text run only (quoted parts are already
    # emitted as phrases and never re-matched).
    expanded: list[str] = []
    buf: list[str] = []
    covered: set[str] = set()

    def flush():
        if not buf:
            return
        text = ' '.join(buf)
        buf.clear()
        pos = 0
        for m in _SYNONYM_RE.finditer(text):
            for w in text[pos:m.start()].split():
                expanded.append(_fts_phrase(w))
            alts = [m.group(1)] + SYNONYMS[m.group(1).lower()]
            covered.update(a.lower() for a in alts)
            expanded.append('(' + ' OR '.join(_fts_phrase(a) for a in alts) + ')')
            pos = m.end()
        for w in text[pos:].split():
            expanded.append(_fts_phrase(w))

    for tok in out:
        if tok.startswith('"') or tok.startswith('('):
            flush()
            expanded.append(tok)
        else:
            buf.append(tok)
    flush()
    # A bare word already inside a synonym group would AND it back in and undo
    # the expansion ("employee share scheme ESS" must not require the token ESS).
    expanded = [t for t in expanded if t.strip('"').lower() not in covered or t.startswith('(')]
    return (' OR ' if operator == "OR" else ' AND ').join(expanded)


def expand_semantic_query(q: str) -> str:
    """Widen a query with tax-terminology synonyms before it is embedded.

    The vector index holds natural-language chunks. A terse fact pattern
    ("crypto staking rewards ordinary income") embeds closer to disposal /
    sale-proceeds rulings than to the ruling that actually answers it, because
    the on-point ruling paraphrases the concept ("cryptocurrency received from
    holding A tokens as ordinary income"). Appending the synonym alternatives
    as plain text pulls the query embedding across the paraphrase. Terms that
    already appear in the query are not repeated. `_expand_query` is the
    FTS5-syntax equivalent for the keyword paths.
    """
    q = (q or "").strip()
    if not q:
        return q
    low = q.lower()
    extra: list[str] = []
    seen: set[str] = set()
    for m in _SYNONYM_RE.finditer(q):
        for alt in SYNONYMS.get(m.group(1).lower(), ()):
            a = alt.lower()
            if a not in seen and a not in low:
                seen.add(a)
                extra.append(alt)
    return f"{q} {' '.join(extra)}" if extra else q


# Title matches outrank body-only matches. Columns: act, section, title, content.
_BM25 = "bm25(sections_fts, 0.0, 1.0, 10.0, 1.0)"

_CITATION_RE = re.compile(
    r'^\s*(?:s|ss|sec|section)?\s*\.?\s*(\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?(?:\(\d+[A-Za-z]*\))*)\s*$',
    re.IGNORECASE,
)


def search_section_ids(q: str, act: str | None = None, limit: int = 20,
                        acts: "Collection[str] | None" = None) -> list[dict]:
    """Citation-style lookup: 's 118-110', '118-110', 's 6(1)' → exact then prefix
    matches on the section id. Returns [] for anything that isn't a citation.

    `act` filters to one Act; `acts` (a scope's Act set) filters to any of several —
    both are pushed into the WHERE clause, before LIMIT. `act` wins if both given.
    """
    m = _CITATION_RE.match(q)
    if not m:
        return []
    sid = m.group(1)
    extra_where = ""
    extra_params: list = []
    if act:
        extra_where = "AND act = ? "
        extra_params = [act]
    elif acts:
        acts = list(acts)
        extra_where = f"AND act IN ({','.join('?' * len(acts))}) "
        extra_params = acts
    sql = (
        "SELECT act, section, title, part, division FROM sections_meta "
        "WHERE (section = ? COLLATE NOCASE OR section LIKE ? || '%' COLLATE NOCASE) "
        + extra_where
        + "ORDER BY (section = ? COLLATE NOCASE) DESC, length(section), act LIMIT ?"
    )
    params = [sid, sid, *extra_params, sid, limit]
    with search_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {"act": r["act"], "section": r["section"], "title": r["title"],
         "part": r["part"], "division": r["division"], "snippet": ""}
        for r in rows
    ]


def search_sections(q: str, act: str | None = None, limit: int = 50,
                    operator: str = "AND",
                    acts: "Collection[str] | None" = None) -> dict:
    """Search using SQLite FTS5 with BM25 ranking.

    Supports double-quoted phrase matching for exact literal search
    (e.g. '"distributable surplus"' matches sections containing that
    exact substring via LIKE, bypassing FTS5 stemming entirely).
    Unquoted multi-word queries use standard FTS5 matching: AND by
    default, OR when operator='OR'.
    """
    # act filters to one Act; acts (a scope's Act set) filters to any of several —
    # both are pushed into the WHERE clause, before LIMIT. act wins if both given.
    act_where = ""
    act_params: list = []
    if act:
        act_where = " AND {col} = ?"
        act_params = [act]
    elif acts:
        acts = list(acts)
        act_where = f" AND {{col}} IN ({','.join('?' * len(acts))})"
        act_params = acts

    # Detect fully quoted phrase — use LIKE for exact literal match
    phrase_match = re.match(r'^\s*"(.+)"\s*$', q)
    if phrase_match:
        exact_q = phrase_match.group(1)
        with search_conn() as conn:
            rows = conn.execute(
                "SELECT s.act, s.section, s.title, "
                "m.part, m.division, "
                "0.0 as rank, '' as snippet "
                "FROM sections_fts s "
                "JOIN sections_meta m ON s.act = m.act AND s.section = m.section "
                "WHERE (s.content LIKE ? OR s.title LIKE ?)" + act_where.format(col="s.act") + " "
                "ORDER BY CASE WHEN s.title LIKE ? THEN 0 ELSE 1 END "
                "LIMIT ?",
                (f"%{exact_q}%", f"%{exact_q}%", *act_params, f"%{exact_q}%", limit),
            ).fetchall()
            total_count = conn.execute(
                "SELECT COUNT(*) FROM sections_fts WHERE (content LIKE ? OR title LIKE ?)"
                + act_where.format(col="act"),
                (f"%{exact_q}%", f"%{exact_q}%", *act_params),
            ).fetchone()[0]
        results = []
        for row in rows:
            results.append({
                "act": row["act"],
                "section": row["section"],
                "title": row["title"],
                "part": row["part"],
                "division": row["division"],
                "snippet": "",
            })
        return {"results": results, "total_count": total_count}

    # Standard FTS5 token-based matching
    if not q.strip():
        return {"results": [], "total_count": 0}
    q_clean = _expand_query(q, operator)
    if not q_clean:
        return {"results": [], "total_count": 0}

    with search_conn() as conn:
        count_row = conn.execute(
            "SELECT COUNT(*) FROM sections_fts WHERE sections_fts MATCH ?"
            + act_where.format(col="sections_fts.act"),
            (q_clean, *act_params)
        ).fetchone()
        total_count = count_row[0] if count_row else 0
        sql = """
            SELECT sections_fts.act, sections_fts.section, sections_fts.title,
                   m.part, m.division,
                   bm25(sections_fts, 0.0, 1.0, 10.0, 1.0) as rank, snippet(sections_fts, 3, '<mark>', '</mark>', '...', 32) as snippet
            FROM sections_fts
            JOIN sections_meta m ON sections_fts.act = m.act AND sections_fts.section = m.section
            WHERE sections_fts MATCH ?""" + act_where.format(col="sections_fts.act") + """
            ORDER BY bm25(sections_fts, 0.0, 1.0, 10.0, 1.0)
            LIMIT ?
        """
        rows = conn.execute(sql, (q_clean, *act_params, limit)).fetchall()

    results = []
    for row in rows:
        results.append({
            "act": row["act"],
            "section": row["section"],
            "title": row["title"],
            "part": row["part"],
            "division": row["division"],
            "snippet": row["snippet"] or "",
        })

    # If query looks like a section number, exact-match it to rank 1
    section_re = re.match(r'^(\d+[A-Za-z]*-\d+(?:[A-Za-z]*(?:\(\d+(?:\)[a-z])?\))?)?)$', q.strip())
    if section_re:
        section_id = section_re.group(1)
        with search_conn() as conn:
            exact = conn.execute(
                "SELECT act, section, sections_fts.title, part, division, "
                "snippet(sections_fts, 3, '<mark>', '</mark>', '...', 32) as snippet "
                "FROM sections_fts JOIN sections_meta m USING(act, section) "
                "WHERE sections_fts.section = ?" + act_where.format(col="sections_fts.act"),
                (section_id, *act_params)
            ).fetchone()
        if exact:
            results = [
                {
                    "act": exact["act"],
                    "section": exact["section"],
                    "title": exact["title"],
                    "part": exact["part"],
                    "division": exact["division"],
                    "snippet": exact["snippet"] or "",
                }
            ] + [
                r for r in results
                if not (r["act"] == exact["act"] and r["section"] == exact["section"])
            ]

    return {"results": results[:limit], "total_count": total_count}


_PREFIX_TYPE_MAP = {
    "TR": "Taxation Ruling",
    "TD": "Taxation Determination",
    "IT": "Taxation Ruling",
    "CR": "Class Ruling",
    "GSTR": "Goods and Services Tax Ruling",
    "LCG": "Law Companion Ruling",
    "PCG": "Practical Compliance Guideline",
    "MT": "Miscellaneous Taxation Ruling",
    "PR": "Product Ruling",
    "PS": "Practice Statement Law Administration",
    "PSLA": "Practice Statement Law Administration",
    "SGR": "Superannuation Guarantee Ruling",
    "TA": "Taxpayer Alert",
    "AID": "ATO Interpretative Decision",
}

def _citation_to_display(citation: str) -> str:
    """Convert internal citation format to display format.
    e.g. 'TR_2012_1' → 'TR 2012/1', 'IT_342' → 'IT 342', 'TD_2026_D1' → 'TD 2026/D1',
    'TR_98_17' → 'TR 98/17', 'PS_LA_2004_11' and 'PSLA_2004_11' → 'PS LA 2004/11'
    (a two-word series used to fall through to 'PS LA 2004 11').
    """
    m = re.match(r'^([A-Za-z]+(?:_[A-Za-z]+)*)_(\d{2}|\d{4})_(D?\d+)$', citation)
    if m:
        return f"{re.sub(r'^PS_?LA$', 'PS LA', m[1].upper()).replace('_', ' ')} {m[2]}/{m[3]}"
    # Number-based citations: IT_342 → IT 342
    m = re.match(r'^([A-Za-z]+)_(\d+)$', citation)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return citation.replace("_", " ")

def _ruling_type_from_citation(citation: str) -> str:
    """Derive the canonical ruling type from the citation prefix."""
    m = re.match(r'^([A-Za-z]+)', citation.strip())
    if m:
        return _PREFIX_TYPE_MAP.get(m.group(1).upper(), "")
    return ""


def search_private_rulings_fts(q: str, limit: int = 20, operator: str = "AND",
                                date_from: str | None = None, date_to: str | None = None,
                                outcome: str | None = None) -> list[dict]:
    """FTS5 term search over private ruling bodies (CDN-0117: term search).

    Matches body content (formatted_text, reasons_for_decision, qa_pairs,
    relevant_legislation) as well as name/authnum. Falls back to LIKE for
    section-style queries FTS5 porter tokenization misses (e.g. "106-60").

    date_from/date_to are pushed into the SQL WHERE clause, before LIMIT.
    outcome (from the outcomes JSON store, not the SQL schema) is applied
    against a wider candidate window fetched before LIMIT, then the result
    is sliced to `limit` — so an outcome-scoped query isn't left choosing
    from whatever happened to land in an unfiltered top-`limit` hit list.
    """
    q = (q or "").strip()
    if not q:
        return []
    tokens = q.split()
    quoted = []
    for tok in tokens:
        if tok.endswith("*") and len(tok) > 1:
            quoted.append('"' + tok[:-1].replace('"', '""') + '"*')
        else:
            quoted.append('"' + tok.replace('"', '""') + '"')
    q_clean = (' OR ' if operator == "OR" else ' ').join(quoted)

    extra_where = ""
    extra_params: list = []
    if date_from:
        extra_where += " AND (m.date_of_advice IS NULL OR m.date_of_advice >= ?)"
        extra_params.append(date_from)
    if date_to:
        extra_where += " AND (m.date_of_advice IS NULL OR m.date_of_advice <= ?)"
        extra_params.append(date_to)

    # outcome isn't in the SQL schema — over-fetch a candidate window so the
    # Python-side outcome filter below has enough rows to fill `limit` from.
    fetch_limit = max(limit * 10, 500) if outcome else limit

    results = []
    try:
        with search_conn() as conn:
            rows = conn.execute(
                f"SELECT private_rulings_fts.authnum, private_rulings_fts.name, "
                "m.year, m.date_of_advice, rank, "
                "snippet(private_rulings_fts, 2, '<mark>', '</mark>', '...', 40) as snippet "
                "FROM private_rulings_fts "
                "JOIN private_rulings_meta m ON private_rulings_fts.authnum = m.authnum "
                f"WHERE private_rulings_fts MATCH ?{extra_where} "
                "ORDER BY rank LIMIT ?",
                [q_clean, *extra_params, fetch_limit],
            ).fetchall()
            for row in rows:
                results.append({
                    "act": "private-rulings",
                    "section": row["authnum"],
                    "title": row["name"] or row["authnum"],
                    "year": row["year"],
                    "date": row["date_of_advice"],
                    "snippet": row["snippet"] or f"Private ruling {row['authnum']}",
                    "source_type": "private_ruling",
                    "type": "private_ruling",
                })
            # LIKE fallback for punctuation-heavy queries FTS5 misses
            if not results:
                like_pat = f"%{q}%"
                like_rows = conn.execute(
                    f"SELECT private_rulings_fts.authnum, private_rulings_fts.name, "
                    "m.year, m.date_of_advice, 0.0 as rank, '' as snippet "
                    "FROM private_rulings_fts "
                    "JOIN private_rulings_meta m ON private_rulings_fts.authnum = m.authnum "
                    "WHERE (private_rulings_fts.content LIKE ? OR private_rulings_fts.name LIKE ? "
                    f"OR private_rulings_fts.authnum LIKE ?){extra_where} "
                    "LIMIT ?",
                    [like_pat, like_pat, like_pat, *extra_params, fetch_limit],
                ).fetchall()
                for row in like_rows:
                    results.append({
                        "act": "private-rulings",
                        "section": row["authnum"],
                        "title": row["name"] or row["authnum"],
                        "year": row["year"],
                        "date": row["date_of_advice"],
                        "snippet": f"Private ruling {row['authnum']} — {row['name']}",
                        "source_type": "private_ruling",
                        "type": "private_ruling",
                    })
    except Exception:
        logger.exception("Private rulings FTS search failed")

    if outcome:
        from backend.services import private_ruling_outcomes
        want = outcome.strip().lower()
        if want in private_ruling_outcomes.VALID_OUTCOMES:
            results = [
                r for r in results
                if private_ruling_outcomes.outcome_matches(
                    (private_ruling_outcomes.get(r["section"]) or {}).get("outcome") or "", want)
            ]

    return results[:limit]


def search_rulings(q: str, limit: int = 20, operator: str = "AND",
                    rtype: set[str] | None = None,
                    date_from: str | None = None, date_to: str | None = None,
                    status: str = "all") -> list[dict]:
    """Search rulings using FTS5 BM25 ranking with exact-match boost.

    rtype (ruling series, e.g. {"PCG"}; exact match, see
    searchd_client.ruling_series) and the year range implied by
    date_from/date_to are applied in the SQL WHERE clause, before LIMIT —
    a scoped or dated query must not be left choosing from whatever
    happened to land in an unfiltered top-`limit` FTS hit list.

    status: "all" (default) or "current" — "current" excludes withdrawn /
    archived / superseded rulings.
    """
    tokens = q.split()
    if not tokens:
        return []

    extra_where = ""
    extra_params: list = []
    if status == "current":
        extra_where += " AND m.withdrawn = 0"
    rtype = normalize_series(rtype)
    if rtype:
        extra_where += f" AND ruling_series(m.citation) IN ({','.join('?' * len(rtype))})"
        extra_params += sorted(rtype)
    year_from = int(date_from[:4]) if date_from else None
    year_to = int(date_to[:4]) if date_to else None
    if year_from is not None:
        extra_where += " AND (m.year IS NULL OR m.year >= ?)"
        extra_params.append(year_from)
    if year_to is not None:
        extra_where += " AND (m.year IS NULL OR m.year <= ?)"
        extra_params.append(year_to)

    # --- Exact citation match (boost to rank 1) ---
    # Normalize spaces/slashes to underscore format stored in DB
    # e.g. "IT 342" → "IT_342", "TR 2012/1" → "TR_2012_1"
    norm = q.strip().replace(" ", "_").replace("/", "_")
    exact_row = None
    try:
        with search_conn() as conn:
            exact_row = conn.execute(
                "SELECT citation, title, year, ruling_type, withdrawn FROM rulings_meta WHERE citation = ?",
                (norm,)
            ).fetchone()
    except Exception:
        pass
    if exact_row and rtype and ruling_series(exact_row["citation"]) not in rtype:
        exact_row = None  # an exact citation outside the requested series

    # --- FTS5 search ---
    quoted = []
    for tok in tokens:
        if tok.endswith('*') and len(tok) > 1:
            inner = tok[:-1].replace('"', '""')
            quoted.append(f'"{inner}"*')
        else:
            quoted.append('"' + tok.replace('"', '""') + '"')
    q_clean = (' OR ' if operator == "OR" else ' ').join(quoted)

    with search_conn() as conn:
        sql = f"""SELECT rulings_fts.citation, rulings_fts.title,
                       m.year, m.ruling_type, m.withdrawn,
                       rank, snippet(rulings_fts, 2, '<mark>', '</mark>', '...', 32) as snippet
                FROM rulings_fts
                JOIN rulings_meta m ON rulings_fts.citation = m.citation
                WHERE rulings_fts MATCH ?{extra_where}
                ORDER BY rank
                LIMIT ?"""
        rows = conn.execute(sql, [q_clean, *extra_params, limit]).fetchall()

    # --- LIKE fallback for citation-bound queries FTS5 misses ---
    # FTS5 porter tokenizer treats underscores as part of the token,
    # so "IT 342" (tokenized as "it", "342") won't match "IT_342" (token "IT_342").
    # Fall back to LIKE on citation and content when FTS returns nothing
    # or the query looks like a ruling reference.
    if len(rows) == 0 or re.match(r'^[A-Za-z]+[\s_/]', q.strip()):
        try:
            with search_conn() as conn:
                like_pat = f"%{norm.replace('_', '%')}%"
                like_sql = f"""SELECT r.citation, r.title, m.year, m.ruling_type, m.withdrawn,
                              0.0 as rank, '' as snippet
                       FROM rulings_fts r
                       JOIN rulings_meta m ON r.citation = m.citation
                       WHERE (r.citation LIKE ? OR r.content LIKE ?){extra_where}
                       ORDER BY CASE WHEN r.citation = ? THEN 0
                                     WHEN r.citation LIKE ? THEN 1
                                     ELSE 2 END
                       LIMIT ?"""
                like_rows = conn.execute(
                    like_sql,
                    [like_pat, like_pat, *extra_params, norm,
                     f"{norm.split('_')[0] if '_' in norm else norm}%", limit],
                ).fetchall()
                if like_rows:
                    # Merge: insert LIKE results that aren't already in FTS results
                    existing = {r[0] for r in rows}
                    for lr in like_rows:
                        if lr[0] not in existing:
                            rows.append(lr)
                            existing.add(lr[0])
        except Exception:
            pass

    results = []
    for row in rows:
        title = row["title"]
        if title == row["citation"]:
            summary_dir = RULING_DIR / "summaries"
            summ_path = summary_dir / f'{row["citation"].replace("/", "_")}.json'
            if summ_path.exists():
                try:
                    meta = json.loads(summ_path.read_text(encoding="utf-8"))
                    title = meta.get("title", meta.get("subject", title))
                except Exception:
                    pass
        results.append({
            "act": "rulings",
            "section": _citation_to_display(row["citation"]),
            "title": title,
            "citation": row["citation"],
            "year": row["year"],
            "ruling_type": _ruling_type_from_citation(row["citation"]) or row["ruling_type"],
            "status": "withdrawn" if row["withdrawn"] else "current",
            "snippet": row["snippet"] or "",
        })

    # Pin exact match to rank 1 if found
    if exact_row:
        exact_citation = exact_row["citation"]
        # Remove any existing entry for the exact match. Compare RAW citations:
        # r["section"] is the display form ("TD 2026/D1") which never equals the
        # stored form ("TD_2026_D1"), so comparing against it left duplicates.
        results = [r for r in results if r.get("citation") != exact_citation]
        title = exact_row["title"]
        if title == exact_citation:
            summary_dir = RULING_DIR / "summaries"
            summ_path = summary_dir / f'{exact_citation.replace("/", "_")}.json'
            if summ_path.exists():
                try:
                    meta = json.loads(summ_path.read_text(encoding="utf-8"))
                    title = meta.get("title", meta.get("subject", title))
                except Exception:
                    pass
        results.insert(0, {
            "act": "rulings",
            "section": _citation_to_display(exact_citation),
            "title": title,
            "citation": exact_citation,
            "year": exact_row["year"],
            "ruling_type": _ruling_type_from_citation(exact_citation) or exact_row["ruling_type"],
            "status": "withdrawn" if exact_row["withdrawn"] else "current",
            "snippet": "",
        })

    return results[:limit]


def search_cases(q: str, limit: int = 20) -> list[dict]:
    """Search case summaries using FTS5 BM25 ranking."""
    tokens = q.split()
    if not tokens:
        return []
    quoted = []
    for tok in tokens:
        if tok.endswith('*') and len(tok) > 1:
            inner = tok[:-1].replace('"', '""')
            quoted.append(f'"{inner}"*')
        else:
            quoted.append('"' + tok.replace('"', '""') + '"')
    q_clean = ' '.join(quoted)

    with search_conn() as conn:
        try:
            sql = """
                SELECT citation, case_name, court,
                       rank, snippet(case_summaries_fts, 3, '<mark>', '</mark>', '...', 32) as snippet
                FROM case_summaries_fts
                WHERE case_summaries_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            rows = conn.execute(sql, (q_clean, limit)).fetchall()
        except Exception:
            rows = []
    results = []
    for row in rows:
        results.append({
            "act": "tax-cases",
            "section": row["citation"],
            "title": row["case_name"],
            "court": court_name_from_citation(row["citation"]) or row["court"],
            "snippet": row["snippet"] or "",
        })
    return results


def search_treaties(q: str, limit: int = 20) -> dict:
    """Search treaty articles using FTS5 BM25 ranking."""
    tokens = q.split()
    if not tokens:
        return {"results": [], "total": 0}
    quoted = []
    for tok in tokens:
        if tok.endswith('*') and len(tok) > 1:
            inner = tok[:-1].replace('"', '""')
            quoted.append(f'"{inner}"*')
        else:
            quoted.append('"' + tok.replace('"', '""') + '"')
    q_clean = ' '.join(quoted)

    with search_conn() as conn:
        count_row = conn.execute(
            "SELECT COUNT(*) FROM treaties_fts WHERE treaties_fts MATCH ?",
            (q_clean,)
        ).fetchone()
        total = count_row[0] if count_row else 0
        sql = """
            SELECT treaties_fts.country, treaties_fts.article, treaties_fts.title,
                   m.country_slug, m.slug,
                   rank, snippet(treaties_fts, 3, '<mark>', '</mark>', '...', 32) as snippet
            FROM treaties_fts
            JOIN treaties_meta m ON treaties_fts.article = m.article AND treaties_fts.country = m.country
            WHERE treaties_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        rows = conn.execute(sql, (q_clean, limit)).fetchall()

    results = []
    for row in rows:
        results.append({
            "country": row["country"],
            "country_slug": row["country_slug"],
            "article": int(row["article"]) if row["article"] else 0,
            "title": row["title"],
            "slug": row["slug"],
            "snippet": row["snippet"] or "",
        })
    return {"results": results, "total": total}


def search_rgs(q: str, limit: int = 20) -> list[dict]:
    """Search ASIC regulatory guides using FTS5 BM25 ranking."""
    tokens = q.split()
    if not tokens:
        return []
    quoted = []
    for tok in tokens:
        if tok.endswith('*') and len(tok) > 1:
            quoted.append(f'"{tok[:-1]}"*')
        else:
            quoted.append('"' + tok.replace('"', '"') + '"')
    q_clean = ' '.join(quoted)
    with search_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT rg, title, rank, "
                "snippet(regulatory_guides_fts, 2, '<mark>', '</mark>', '...', 32) as snippet "
                "FROM regulatory_guides_fts WHERE regulatory_guides_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (q_clean, limit)
            ).fetchall()
        except Exception:
            rows = []
    results = []
    for row in rows:
        try:
            rg_num = int(row["rg"])
        except (TypeError, ValueError):
            rg_num = row["rg"]
        results.append({
            "rg": rg_num,
            "title": row["title"],
            "snippet": row["snippet"] or "",
        })
    return results


def search_afsa_guides(q: str, limit: int = 20) -> list[dict]:
    """Search AFSA practice guidance using FTS5 BM25 ranking."""
    tokens = q.split()
    if not tokens:
        return []
    quoted = []
    for tok in tokens:
        if tok.endswith('*') and len(tok) > 1:
            quoted.append(f'"{tok[:-1]}"*')
        else:
            quoted.append('"' + tok.replace('"', '"') + '"')
    q_clean = ' '.join(quoted)
    with search_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT slug, title, rank, "
                "snippet(afsa_guides_fts, 2, '<mark>', '</mark>', '...', 32) as snippet "
                "FROM afsa_guides_fts WHERE afsa_guides_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (q_clean, limit)
            ).fetchall()
        except Exception:
            rows = []
    results = []
    for row in rows:
        results.append({
            "slug": row["slug"],
            "title": row["title"],
            "snippet": row["snippet"] or "",
        })
    return results


def normalize_citation(s: str) -> str:
    """Convert a filename-style slug ('1970_HCA_23') to its medium-neutral
    form ('[1970] HCA 23'). Idempotent: already-formatted citations pass
    through unchanged. Used by search_cases_fts so slugs never surface to
    the MCP/UI."""
    if not s:
        return s
    parts = s.split("_")
    if (len(parts) == 3 and len(parts[0]) == 4 and parts[0].isdigit()
            and parts[1].isalpha() and parts[1].isupper() and parts[2].isdigit()):
        return f"[{parts[0]}] {parts[1]} {parts[2]}"
    return s


def search_cases_fts(q: str, limit: int = 20, operator: str = "AND") -> list[dict]:
    """Search case summaries using FTS5 BM25 ranking.

    Returns list of dicts with citation, case_name, court, has_summary=True.
    """
    tokens = q.split()
    if not tokens:
        return []
    quoted = []
    for tok in tokens:
        if tok.endswith('*') and len(tok) > 1:
            inner = tok[:-1].replace('"', '""')
            quoted.append(f'"{inner}"*')
        else:
            quoted.append('"' + tok.replace('"', '""') + '"')
    q_clean = (' OR ' if operator == "OR" else ' ').join(quoted)
    from urllib.parse import quote

    with search_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT citation, case_name, court, rank "
                "FROM case_summaries_fts WHERE case_summaries_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (q_clean, limit)
            ).fetchall()
        except Exception:
            rows = []

        # If strict quoted-AND query returns nothing, fall back to unquoted OR
        if not rows and len(tokens) > 1:
            try:
                loose = ' OR '.join(t.replace('-', ' ').replace('"', '""') for t in tokens)
                rows = conn.execute(
                    "SELECT citation, case_name, court, rank "
                    "FROM case_summaries_fts WHERE case_summaries_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (loose, limit)
                ).fetchall()
            except Exception:
                rows = []

    results = []
    for row in rows:
        citation = normalize_citation(row["citation"])
        results.append({
            "citation": citation,
            "case_name": row["case_name"],
            "court": court_name_from_citation(citation) or row["court"],
            "year": citation[1:5] if citation.startswith("[") else "",
            "has_summary": True,
            "html_url": f"{os.environ.get('APP_BASE_URL', 'https://lawkitty.app')}/app/tax-cases/{quote(citation)}",
        })
    return results

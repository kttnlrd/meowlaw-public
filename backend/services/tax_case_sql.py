"""Query the internal case PostgreSQL database for tax case data.

Uses docker exec to run queries inside the lawkitty-postgres container.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from typing import Any

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

_PSQL = [
    "docker", "exec", "-i", "lawkitty-postgres",
    "psql", "-U", "postgres", "-d", "lawkitty_cases",
    "-t", "-F", "\x01", "-A",  # tuples-only, SOH-sep, unaligned
]

# Module-level psycopg2 connection for fast reads
_reader_conn: object | None = None
_reader_lock = threading.Lock()


def _get_reader() -> object:
    """Return a shared psycopg2 connection (reuse, not docker exec)."""
    global _reader_conn
    with _reader_lock:
        if _reader_conn is not None:
            try:
                cur = _reader_conn.cursor()
                cur.execute("SELECT 1")
                cur.close()
                return _reader_conn
            except Exception:
                # Connection stale — reconnect
                try:
                    _reader_conn.close()
                except Exception:
                    pass
                _reader_conn = None
        try:
            import os
            host = os.environ.get("PGHOST", "127.0.0.1")
            port = int(os.environ.get("PGPORT", "5432"))
            user = os.environ.get("PGUSER", "postgres")
            password = os.environ.get("PGPASSWORD", "")
            dbname = os.environ.get("PGDATABASE", "lawkitty_cases")
            _reader_conn = psycopg2.connect(
                host=host, port=port, user=user, password=password, dbname=dbname,
                connect_timeout=5,
            )
            _reader_conn.set_session(autocommit=True, readonly=True)
        except Exception:
            _reader_conn = None
        return _reader_conn


def _sql(query: str, params: tuple | None = None) -> list[list[str]]:
    """Run a SQL query via psycopg2 (fast, no docker exec overhead).

    Values are passed as ``params`` (%s placeholders) — never interpolated
    into ``query``. Falls back to docker exec on failure; the fallback cannot
    bind parameters, so it refuses to run parameterized queries at all.
    Returns rows as list of string lists (same format as before).
    """
    conn = _get_reader()
    if conn is not None:
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            rows = [[str(cell) if cell is not None else "" for cell in row] for row in cur.fetchall()]
            cur.close()
            return rows
        except Exception as e:
            logger.warning(f"psycopg2 query failed, falling back to docker exec: {e}")
    # Fallback: docker exec. It cannot bind params, so never run a
    # parameterized query through it (would execute %s literally).
    if params:
        logger.warning("docker exec fallback cannot bind params — returning no rows")
        return []
    try:
        result = subprocess.run(
            _PSQL + ["-c", query],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.warning(f"SQL query failed: {result.stderr[:200]}")
            return []
        return _parse_rows(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning("SQL query timed out")
        return []
    except FileNotFoundError:
        logger.warning("docker not available")
        return []
    except Exception as e:
        logger.exception(f"SQL query error: {e}")
        return []


def _parse_rows(output: str) -> list[list[str]]:
    """Parse psql -t -F $'\\x01' -A output into list of string lists."""
    rows = []
    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue
        rows.append([cell.strip() for cell in line.split("\x01")])
    return rows


def _to_dict(columns: list[str], row: list[str]) -> dict[str, Any]:
    """Map a row of values to column names, with type inference."""
    result = {}
    for i, col in enumerate(columns):
        val = row[i] if i < len(row) else None
        if val is None or val == "" or val == "NULL":
            result[col] = None
        else:
            # Try to parse known types
            result[col] = _infer_type(val)
    return result


def _infer_type(val: str) -> Any:
    """Infer the type of a psql string value."""
    # JSON object (must check before PG array literal since both use {})
    if val.startswith("{\"") and val.endswith("}"):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    # PostgreSQL array literal: {value1,value2}
    if val.startswith("{") and val.endswith("}"):
        return _parse_pg_array(val)
    # JSON array
    if val.startswith("[") and val.endswith("]"):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    # Number
    try:
        if "." in val:
            return float(val)
        return int(val)
    except (ValueError, TypeError):
        pass
    return val


def _parse_pg_array(val: str) -> list[Any]:
    """Parse PostgreSQL array literal like {value1,value2}."""
    inner = val[1:-1]
    if not inner.strip():
        return []
    items = []
    current: list[str] = []
    in_quotes = False
    for ch in inner:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            items.append("".join(current).strip().strip('"'))
            current = []
        else:
            current.append(ch)
    if current:
        items.append("".join(current).strip().strip('"'))
    return items


def _sql_write(sql: str) -> bool:
    """Run an INSERT/UPDATE/DELETE SQL statement via docker exec.

    Returns True on success, False on failure.
    Uses default psql format (no -t -F -A flags needed for writes).
    """
    try:
        result = subprocess.run(
            ["docker", "exec", "lawkitty-postgres", "psql", "-U", "postgres",
             "-d", "lawkitty_cases", "-c", sql],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logger.warning(f"SQL write failed: {result.stderr[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("SQL write timed out")
        return False
    except FileNotFoundError:
        logger.warning("docker not available")
        return False
    except Exception as e:
        logger.exception(f"SQL write error: {e}")
        return False


def _conn() -> object:
    """Return a psycopg2 connection to lawkitty_cases via docker exec.

    The container must expose port 5432 on localhost, or we connect via
    docker exec. For parameterized writes we use the direct psycopg2 route.
    """
    import os
    host = os.environ.get("PGHOST", "127.0.0.1")
    port = int(os.environ.get("PGPORT", "5432"))
    user = os.environ.get("PGUSER", "postgres")
    password = os.environ.get("PGPASSWORD", "")
    dbname = os.environ.get("PGDATABASE", "lawkitty_cases")
    return psycopg2.connect(
        host=host, port=port, user=user, password=password, dbname=dbname,
        connect_timeout=5,
    )


def _sql_write_params(sql: str, params: tuple = ()) -> bool:
    """Execute SQL with parameterized values via psycopg2.

    All user-supplied values should be passed as params, not interpolated
    into the SQL string.  This prevents SQL injection.

    Example:
        _sql_write_params(
            "INSERT INTO issues (ticket, category) VALUES (%s, %s)",
            ("CDN-0001", "bad_data"),
        )

    Returns True on success, False on failure.
    """
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        return True
    except Exception as e:
        logger.exception(f"SQL write (params) error: {e}")
        return False


def _sql_dict(columns: list[str], query: str, params: tuple | None = None) -> list[dict[str, Any]]:
    """Run SQL and return results as list of dicts with given column names."""
    rows = _sql(query, params)
    return [_to_dict(columns, row) for row in rows]


def get_case_sql_data(citation: str) -> dict[str, Any] | None:
    """Fetch SQL-stored data for a case by citation.

    Returns None if not found or DB unavailable.
    """
    # 1. Find the document
    docs = _sql_dict(
        ["id", "reference", "title", "content_length"],
        "SELECT id, reference, title, LENGTH(content) FROM documents "
        "WHERE doc_type='case' AND reference = %s LIMIT 1",
        (citation,),
    )
    if not docs:
        return None

    doc = docs[0]
    doc_id = doc.get("id")
    if not doc_id:
        return None

    # 2. Get content preview
    preview_rows = _sql_dict(
        ["preview"],
        "SELECT LEFT(content, 2000) FROM documents WHERE id = %s",
        (doc_id,),
    )
    preview = preview_rows[0]["preview"] if preview_rows else ""

    # 3. Get chunk info
    chunks = _sql_dict(
        ["chunk_index", "content_length"],
        "SELECT chunk_index, LENGTH(content) FROM chunks "
        "WHERE document_id = %s ORDER BY chunk_index",
        (doc_id,),
    )

    # 4. Get case metadata from cases table
    case_meta_rows = _sql_dict(
        ["citation", "case_name", "court", "decision_date", "judges", "outcome",
         "related_provisions", "related_rulings", "head_notes"],
        "SELECT citation, case_name, court, decision_date::text, judges, outcome, "
        "related_provisions, related_rulings, head_notes::text "
        "FROM cases WHERE document_id = %s LIMIT 1",
        (doc_id,),
    )
    case_meta = case_meta_rows[0] if case_meta_rows else {}

    # Clean up array fields
    for arr_field in ["judges", "related_provisions", "related_rulings"]:
        if isinstance(case_meta.get(arr_field), list):
            case_meta[arr_field] = [str(s).strip('"') for s in case_meta[arr_field]]
        elif case_meta.get(arr_field) is None:
            case_meta[arr_field] = []

    # Parse head_notes JSON
    if isinstance(case_meta.get("head_notes"), str):
        try:
            case_meta["head_notes"] = json.loads(case_meta["head_notes"])
        except (json.JSONDecodeError, TypeError):
            pass

    # 5. Get paragraphs
    paragraphs = _sql_dict(
        ["paragraph_number", "paragraph_label", "section_type", "content_preview", "content_length", "sequence_order"],
        "SELECT paragraph_number, paragraph_label, section_type, "
        "LEFT(content, 500), LENGTH(content), sequence_order "
        "FROM case_paragraphs "
        "WHERE case_id IN (SELECT id FROM cases WHERE document_id = %s) "
        "ORDER BY sequence_order NULLS LAST, paragraph_number",
        (doc_id,),
    )

    return {
        "slug": doc_id,
        "full_content_length": doc.get("content_length"),
        "full_content_preview": preview,
        "paragraph_count": len(paragraphs),
        "paragraphs": paragraphs,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "case_meta": case_meta,
    }
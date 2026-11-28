"""FastMCP server for Legislation Explorer — replaces old mcp_server.py.

Mount via streamable_http_app() on the main FastAPI app.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import hmac
import json
import logging
import os
import re as _re
import sqlite3
import sys
import time
from pathlib import Path
from contextvars import ContextVar
from typing import Literal

import pydantic_core
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types.version import (
    HANDSHAKE_PROTOCOL_VERSIONS, KNOWN_PROTOCOL_VERSIONS, LATEST_HANDSHAKE_VERSION,
)

from backend.config import DATA_DIR, TREATIES_DIR
from content.registry import tag as _tag_content_type
from backend.doc_ids import STATE_TAX_ACTS, state_tax_act_names
from backend.mcp_token_manager import token_manager
from backend.services import usage_log
from backend.services.content_access import (
    NODE_TYPE_KEY, act_key, available_keys, enabled_keys, gate_key, hit_key, node_allowed,
)
from backend.services.content_access import is_hidden as _hidden

# Private firm content (absent from the public export). Every hook below is skipped
# unless _pkb_on(), so a caller without the grant sees exactly the pre-hook behaviour.
try:
    from backend.services import private_kb as _pkb
except ImportError:
    _pkb = None


# Marks the usage event of a request that presented the static operator token. Set ONLY by
# MCPAuthMiddleware. An object, not a string: nothing read from a request (tool arguments, a
# token-table owner or OAuth email that happens to read "operator") can produce it.
_OPERATOR = object()
_OPERATOR_FIELD = "_gate_operator"

# >0 while this process serves the MCP Streamable HTTP app (main.py lifespan). While it is, a
# tool call with no usage event is an HTTP call whose context was lost, not stdio.
_http_serving = 0


@contextlib.contextmanager
def serving_http():
    """Mark this process as serving MCP over HTTP for the lifetime of the app (see _mcp_owner)."""
    global _http_serving
    _http_serving += 1
    try:
        yield
    finally:
        _http_serving -= 1


_ANONYMOUS = {"email": ""}  # available_keys(): no grants, not an admin


def _mcp_owner() -> dict | None:
    """Caller for content gating; None = not restricted by grants (static operator token /
    stdio). Even then a type switched OFF globally is hidden (_mcp_avail).

    Fails closed. MCPAuthMiddleware gives every HTTP request a usage event, and only the static
    operator token (MCP_AUTH_TOKEN / LEGISLATION_BEARER_TOKEN) marks it _OPERATOR. Any other
    request without a resolved owner (DEV_MODE, an owner lookup that came back empty, an event
    lost on the way to the tool) gets the anonymous view. No event outside HTTP serving is the
    stdio transport or an in-process call.
    """
    ev = usage_log.current.get()
    if ev is None:
        return dict(_ANONYMOUS) if _http_serving else None
    if ev.get(_OPERATOR_FIELD) is _OPERATOR:
        return None
    owner = ev.get("user_email")
    return {"email": owner} if isinstance(owner, str) and owner.strip() else dict(_ANONYMOUS)


# Per tool call: caller -> available keys. One call asks _mcp_avail() many times (get_info once
# per act: ~92 lookups, two Postgres connections each); the answer cannot change within a call.
_avail_memo: ContextVar[dict | None] = ContextVar("mcp_avail_memo", default=None)


def _mcp_avail() -> frozenset[str]:
    """Content keys the MCP caller may read. The operator token and stdio get every key that is
    globally ON; a global OFF hides a type from every caller. Memoised per tool call."""
    owner = _mcp_owner()
    who = None if owner is None else owner["email"]
    memo = _avail_memo.get()
    if memo is not None and who in memo:
        return memo[who]
    avail = frozenset(enabled_keys() if owner is None else available_keys(owner))
    if memo is not None:
        memo[who] = avail
    return avail


def _memoise_avail_per_call(server) -> None:
    """Wrap every registered tool once so each call gets a fresh _mcp_avail() memo."""
    for t in server._tool_manager.list_tools():
        if getattr(t.fn, "_avail_memo", False):
            continue
        fn, is_async = t.fn, t.is_async

        @functools.wraps(fn)
        async def wrapper(*args, __fn=fn, __async=is_async, **kwargs):
            tok = _avail_memo.set({})
            try:
                return (await __fn(*args, **kwargs)) if __async else __fn(*args, **kwargs)
            finally:
                _avail_memo.reset(tok)

        wrapper._avail_memo = True
        t.fn, t.is_async = wrapper, True


def _mcp_filter(rows: list[dict]) -> list[dict]:
    """Drop hits of types unavailable to the MCP caller; untagged rows are kept."""
    avail = _mcp_avail()
    return [r for r in rows if not hit_key(r) or hit_key(r) in avail]


def _mcp_denied(key: str) -> str | None:
    """JSON error when `key` is unavailable to the MCP caller, else None.

    A hidden type never names itself: its denial reads as a plain "not found".
    """
    if key in _mcp_avail():
        return None
    if _hidden(key):
        return json.dumps({"error": "not found"})
    return json.dumps({"error": f"{key} is not available to your account"})


def _mcp_act_key(act: str) -> str:
    """Gate key for an act id. A hidden type keeps its own key, so a caller without it is
    denied and the act is skipped wherever acts are filtered, exactly like an act with no data.
    (It used to map to "legislation", which was only safe while hidden types had no tree.json.)"""
    return act_key(act)


_UNKNOWN_ACT = "zz-no-such-act"


def _mcp_hidden_denied(act: str) -> bool:
    """A hidden type the caller cannot use: every tool must answer as for an act it lacks."""
    k = act_key(act)
    return _hidden(k) and bool(_mcp_denied(k))


def _pkb_on() -> bool:
    """True when the private KB exists here and the MCP caller may read it."""
    if _pkb is None or not _pkb.enabled():
        return False
    return _pkb.KEY in _mcp_avail()
from backend.routes.api import VERSION, CHANGELOG
from backend.routes.rulings import _ruling_payload as _get_ruling
from backend.services.graph_alias import lookup as alias_lookup
from backend.services.data_loader import (
    is_plain_id,
    load_tree,
    load_rulings,
    get_definition_text,
    get_definition_across_acts,
)
from backend.services.text_cleaner import strip_scraped_markup, clean_legislation_body
from backend.services.search_service import (
    search_sections as fts_search,
    search_rulings,
    search_conn,
    search_treaties,
    search_rgs,
)

from backend.routes.regulatory_guides import (
    _load_rg_section_index,
)
from backend.services.case_db_service import (
    build_download_urls,
    get_case_metadata,
    get_case_references,
)
from backend.services.tax_case_sql import _sql, _sql_dict, _sql_write_params

logger = logging.getLogger(__name__)

# Appended to error/null returns of data tools: tells the LLM to re-check
# get_info (query formats, citation conventions, routing) before giving up.
_GET_INFO_HINT = (
    "If this result is unexpected, call get_info to review query formats, "
    "citation conventions, and tool routing, then retry."
)


def _tool_error(code: str, message: str, suggestions: list[str] | None = None) -> ToolError:
    """Return the exception the SDK surfaces as an isError tool result (spec §5).

    It must be ToolError: mcp 2.x sends any other exception to the client as a bare
    "Error executing tool <name>", which would drop the code, message and suggestions.
    """
    return ToolError(json.dumps({"code": code, "message": message, "suggestions": suggestions or []}))


# "RuntimeError" for a ToolError, as before mcp 2.x (see usage_log.error_label).
error_label = usage_log.error_label


def _add_ids(results: dict) -> dict:
    """Attach canonical document IDs to grouped results so they chain into fetch."""
    from backend.doc_ids import leg_id, case_id, ruling_id, afsa_id

    for r in results.get("sections", []):
        r.setdefault("id", leg_id(r.get("act", ""), r.get("section", "")))

    for r in results.get("cases", []):
        try:
            r.setdefault("id", case_id(r.get("citation", "")))
        except (ValueError, TypeError):
            r.setdefault("id", "")

    for r in results.get("rulings", []):
        cit = _re.sub(r"[_/]", " ", r.get("citation", "") or "").strip()
        m = _re.match(r"([A-Z ]+?)\s*(\d{4})\s+(\d+)$", cit)
        if m:
            r.setdefault("id", ruling_id(m.group(1), int(m.group(2)), int(m.group(3))))
        else:
            r.setdefault("id", "")

    for r in results.get("private_rulings", []):
        authnum = str(r.get("authnum") or r.get("id") or "")
        r.setdefault("id", f"pbr:{authnum}" if authnum else "")

    for r in results.get("afsa_guides", []):
        r.setdefault("id", afsa_id(r.get("slug", "")))

    return results

# Public hostnames that hit this server via Cloudflare Tunnel / Caddy.
# Additional hosts can be added with MCP_EXTRA_ALLOWED_HOSTS (comma-separated).
# The SDK auto-enables DNS-rebinding protection (127.0.0.1:*/localhost:* only, so public Host
# headers get 421) when no settings are passed; MCP_TRANSPORT_SECURITY below always passes them.
_MCP_ALLOWED_HOSTS = [
    "127.0.0.1:*",
    "localhost:*",
    "[::1]:*",
    "lawkitty.app",
    "lawkitty.app:*",
    "www.lawkitty.app",
    "www.lawkitty.app:*",
]
# Extra public hostnames (comma-separated, bare hostnames) for non-prod deployments.
for _h in os.environ.get("MCP_EXTRA_ALLOWED_HOSTS", "").split(","):
    _h = _h.strip()
    if _h:
        _MCP_ALLOWED_HOSTS += [_h, f"{_h}:*"]

# Public base for absolute download links in tool output. Override via env for
# non-prod hosts (e.g. LEGISLATION_PUBLIC_BASE=https://dev.example.com).
_PUBLIC_BASE = os.environ.get("LEGISLATION_PUBLIC_BASE", "https://lawkitty.app").rstrip("/")
# SPA deep links live under /app; _PUBLIC_BASE stays host-only for /api download links.
_APP_BASE = f"{_PUBLIC_BASE}/app"

def _abs(path: str) -> str:
    """Turn a relative API path into an absolute public URL."""
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return f"{_PUBLIC_BASE}{path if path.startswith('/') else '/' + path}"
_MCP_ALLOWED_ORIGINS = [
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
    "https://lawkitty.app",
    "https://www.lawkitty.app",
]
# Extra allowed origins (comma-separated, full origins e.g. https://dev.example.com).
for _o in os.environ.get("MCP_EXTRA_ALLOWED_ORIGINS", "").split(","):
    _o = _o.strip()
    if _o:
        _MCP_ALLOWED_ORIGINS.append(_o)

# FastMCP sub-app — Mount strips /mcp prefix, so route must be at "/"
_INSTRUCTIONS = (
    "lawkitty returns Australian and New Zealand tax and corporate law primary sources. "
    "Workflow: search or outline to find document IDs, fetch to read, related to follow citations. "
    "Document IDs: leg:<act>:<section> (e.g. leg:itaa-1997:8-1), leg:<act>:<section>(<n>) for subsections, "
    "NZ IT Act sections leg:nz-it-2007:CB-1 (CB1 also accepted), "
    "case:<year>-<court>-<n>, ruling:<series>-<year>-<n>, pbr:<authnum>, rg:<n> (rg:<n>:<para> for one paragraph), dta:<country>:<article>. "
    "Act slugs: itaa-1997, itaa-1936, gst-1999, fbt-1986, taa-1953, sis-1993, nz-it-2007, "
    "corporations-act-2001, aml-ctf-2006, aml-ctf-rules-2007. "
    "State tax Acts (duties, land tax, payroll tax; unofficial copies, see each fetch's licence): "
    + ", ".join(STATE_TAX_ACTS) + ". "
    "fetch accepts a document ID or a citation (s 8-1 ITAA 1997, TR 2024/1, [2024] HCA 1). "
    "A bare section citation (s 8-1) is ambiguous; name the act. "
    "Private rulings apply only to the applicant and are not precedent."
)

# Passed to mcp.streamable_http_app() in main.py (the 2.x SDK takes transport settings there).
MCP_TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_MCP_ALLOWED_HOSTS,
    allowed_origins=_MCP_ALLOWED_ORIGINS,
)

# mcp 2.x refuses request bodies over max_request_body_size (default 4 MiB) with 413; 1.x had no
# limit. MCPAuthMiddleware has already buffered the whole body by the time the SDK sees it, so the
# SDK limit would save no memory: keep the 1.x behaviour. Passed to streamable_http_app() in main.py.
MCP_MAX_REQUEST_BODY_SIZE = sys.maxsize

# serverInfo.version is the app version (mcp 1.x reported its own SDK version there).
mcp = MCPServer("lawkitty", instructions=_INSTRUCTIONS, version=VERSION)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CASE_CITATION_RE = _re.compile(r'\[(\d{4})\]\s*([A-Z]+(?: [A-Z]+)*)\s*(\d+(?:[-–]\d+)*)')

_CASE_CITATION_BRACKETLESS_RE = _re.compile(r'(\d{4})\s+([A-Z]+(?: [A-Z]+)*)\s+(\d+(?:[-–]\d+)*)')

# Report-series -> medium-neutral alias map (data/atc_alias_map.json).
# Populated only with individually verified case pairs — never mass-generated.
@functools.lru_cache(maxsize=1)
def _load_case_citation_aliases() -> dict:
    p = DATA_DIR / "atc_alias_map.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _resolve_case_citation_alias(citation_norm: str) -> str:
    """Map a normalised report-series citation to its canonical medium-neutral
    citation when a verified alias exists (e.g. '[2009] ATC 1-016' ->
    '[2009] AATA 805'). Returns the input unchanged when no alias is known."""
    if not citation_norm:
        return citation_norm
    return _load_case_citation_aliases().get(citation_norm, citation_norm)

def _normalise_case_citation(raw: str) -> str | None:
    m = _CASE_CITATION_RE.search(raw)
    if m:
        return f"[{m.group(1)}] {m.group(2)} {m.group(3)}"
    m = _CASE_CITATION_BRACKETLESS_RE.search(raw)
    if m:
        return f"[{m.group(1)}] {m.group(2)} {m.group(3)}"
    return None

# ---------------------------------------------------------------------------
# Shared value helpers: ISO dates, public act collections, cursors, chunking
# ---------------------------------------------------------------------------

_ISO_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _to_iso_date(value) -> str | None:
    """Normalise a date to ISO 8601 (YYYY-MM-DD).

    Act trees carry a mix of ISO ('2026-07-01') and long-form
    ('27 August 2026') compilation dates. outline()/get_info() must present
    one format, so every date they surface goes through here.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return s
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = _re.match(r"(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})", s)
    if m:
        mon = _ISO_MONTHS.get(m.group(2)[:3].lower())
        if mon:
            return f"{m.group(3)}-{mon}-{int(m.group(1)):02d}"
    m = _re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


# Non-legislation directories that must never be published as act collections.
_INTERNAL_COLLECTIONS = {"spec"}


@functools.lru_cache(maxsize=1)
def _public_act_entries() -> tuple[dict, ...]:
    """Every public legislation act, with ISO compilation metadata.

    Single source of truth for outline() and get_info(): the internal
    display-spec directory is excluded, and compilation dates are normalised
    to ISO 8601. Cached — a data rebuild requires a service restart.
    """
    acts: list[dict] = []
    for act_dir in sorted(DATA_DIR.iterdir()):
        if not act_dir.is_dir() or act_dir.name in _INTERNAL_COLLECTIONS:
            continue
        if not (act_dir / "tree.json").exists():
            continue
        tree = load_tree(act_dir.name)
        comp_no = tree.get("compilation_no")
        try:
            comp_no = int(comp_no) if comp_no is not None else None
        except (TypeError, ValueError):
            comp_no = None
        acts.append({
            "id": act_dir.name,
            "name": tree.get("act", act_dir.name),
            "compilation_no": comp_no,
            "compilation_date": _to_iso_date(tree.get("compilation_date")),
        })
    return tuple(acts)


def _encode_cursor(payload: dict) -> str:
    """Opaque page cursor: base64url of the next offsets (spec §4.1)."""
    import base64
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> dict:
    """Decode a cursor produced by _encode_cursor; {} when absent/invalid."""
    if not cursor:
        return {}
    import base64
    try:
        pad = "=" * (-len(cursor) % 4)
        return json.loads(base64.urlsafe_b64decode(cursor + pad).decode("utf-8"))
    except Exception:
        return {}


_CHUNK_SIZE = 8000

# Protocol versions, used by MCPAuthMiddleware to down-negotiate. _HANDSHAKE_VERSIONS are the
# revisions reachable through initialize, _LATEST_HANDSHAKE the newest of them. A header outside
# _accepted_versions() is rewritten to _LATEST_HANDSHAKE. Never use the SDK's
# LATEST_PROTOCOL_VERSION here: it is the 2026-07-28 per-request-envelope revision, which
# old-style requests cannot use.
_HANDSHAKE_VERSIONS = tuple(HANDSHAKE_PROTOCOL_VERSIONS)
_LATEST_HANDSHAKE = LATEST_HANDSHAKE_VERSION

# MCP_MODERN_ERA (default off) lets clients onto the 2026-07-28 era. Off: 2026-07-28 is
# down-negotiated like any unknown revision and server/discover gets the -32601 shim, so the
# wire is exactly what mcp 1.26 served. On: 2026-07-28 reaches the SDK's modern path and
# server/discover is answered natively. Turn it on only once the proxies are known to pass
# Mcp-Method / Mcp-Name.
MCP_MODERN_ERA = os.environ.get("MCP_MODERN_ERA", "").strip().lower() in ("1", "true", "yes", "on")


def _accepted_versions() -> tuple[str, ...]:
    """Header values passed to the SDK unchanged."""
    return tuple(KNOWN_PROTOCOL_VERSIONS) if MCP_MODERN_ERA else _HANDSHAKE_VERSIONS


# Request bodies mcp 1.x served and 2.x refuses. 1.x parsed with json.loads (a UTF-8 BOM, UTF-16/32
# and unpaired surrogate escapes are all accepted), typed _meta.progressToken as optional and
# initialize's protocolVersion as str | int. 2.x parses with pydantic_core (400 -32700 for all
# three), rejects "progressToken": null with -32602 and wants protocolVersion as a string.
# MCPAuthMiddleware forwards such a body rewritten into a form 2.x accepts, with the same meaning.
_LONE_SURROGATE = _re.compile("[\ud800-\udfff]")


def _without_lone_surrogates(obj):
    """json.loads keeps an unpaired surrogate escape (a JS string cut inside an emoji) as a lone
    surrogate, which a UTF-8 body cannot carry: replace it with U+FFFD. Pairs are already joined."""
    if isinstance(obj, str):
        return _LONE_SURROGATE.sub("\ufffd", obj)
    if isinstance(obj, list):
        return [_without_lone_surrogates(v) for v in obj]
    if isinstance(obj, dict):
        return {_without_lone_surrogates(k): _without_lone_surrogates(v) for k, v in obj.items()}
    return obj


def _normalise_message(msg) -> bool:
    """Rewrite, in place, the fields of one JSON-RPC message that 1.x accepted and 2.x refuses.
    Returns True if anything changed."""
    params = msg.get("params") if isinstance(msg, dict) else None
    if not isinstance(params, dict):
        return False
    changed = False
    meta = params.get("_meta")
    if isinstance(meta, dict) and "progressToken" in meta and meta["progressToken"] is None:
        # What a serialiser without omit-empty writes for "no progress token".
        del meta["progressToken"]
        changed = True
    pv = params.get("protocolVersion")
    if msg.get("method") == "initialize" and (
            isinstance(pv, int) or (isinstance(pv, float) and pv.is_integer())):
        # 1.x coerced it to int (bools and integral floats included) and, as it is never a known
        # revision, counter-offered its latest; 2.x does the same for the string.
        params["protocolVersion"] = str(int(pv))
        changed = True
    return changed


def _compat_body(body: bytes, parsed) -> bytes | None:
    """The body to forward to the SDK in place of `body` (`parsed` is its json.loads result,
    modified in place), or None to forward `body` unchanged."""
    try:
        pydantic_core.from_json(body)
        strict_ok = True
    except ValueError:
        strict_ok = False
    changed = _normalise_message(parsed)
    if strict_ok and not changed:
        return None
    if not strict_ok:
        parsed = _without_lone_surrogates(parsed)
    try:
        return json.dumps(parsed, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return None


def _parse_chunk_spec(spec, total: int) -> tuple[list[int], str | None]:
    """Turn the fetch ``chunk`` argument into the chunk numbers to return.

    Accepts a single number (``2``), a range (``'2-4'``), a list (``'1,3'`` or
    ``'1-2,5'``) and ``'all'``. De-duplicates while keeping the requested order.

    Asking past the end is CLAMPED, not refused: clients page blindly (observed asking for
    chunk 6, 8 and 21 of documents that have one chunk) and a hard error costs them the whole
    call. The second return value carries a note explaining the clamp so the caller can see
    it got the end of the document rather than the chunk it named. Malformed specs still raise
    ValueError, as does a request where nothing at all is in range.
    """
    if isinstance(spec, bool):  # bool is an int subclass — never a chunk number
        raise ValueError(f"chunk {spec!r} is not a chunk number")
    if isinstance(spec, int):
        raw = str(spec)
    elif isinstance(spec, str):
        raw = spec.strip().lower()
    else:
        raise ValueError(f"chunk {spec!r} is not a number, range, list or 'all'")
    if raw in ("", "all", "*"):
        return list(range(1, total + 1)), None
    wanted: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        m = _re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                raise ValueError(f"chunk range '{part}' runs backwards")
            # Bound the span BEFORE building the range: '1-1000000000' used to allocate a
            # billion-element list, so one call could exhaust memory.
            if hi - lo + 1 > total:
                raise ValueError(
                    f"chunk range '{part}' asks for {hi - lo + 1} chunks but this document "
                    f"has {total}")
            wanted.extend(range(lo, hi + 1))
            continue
        if not _re.fullmatch(r"\d+", part):
            raise ValueError(f"chunk spec '{part}' is not a number, range or 'all'")
        wanted.append(int(part))
    if not wanted:
        raise ValueError("no chunks requested")
    seen: set[int] = set()
    kept: list[int] = []
    for n in wanted:
        if n not in seen:
            seen.add(n)
            kept.append(n)
    in_range = [n for n in kept if 1 <= n <= total]
    if not in_range:
        # Everything asked for is past the end: give the last chunk rather than nothing.
        return ([total] if total else [1]), (
            f"requested chunk {kept[0]} but this document has {total} chunk(s); "
            f"returned chunk {total if total else 1} (the end of the document)")
    dropped = [n for n in kept if n > total]
    note = None
    if dropped:
        note = (f"ignored chunk {dropped[0]} (past the end — this document has {total} "
                f"chunk(s)); returned chunks {in_range[0]}-{in_range[-1]}")
    return in_range, note


def _split_into_chunks(text: str, size: int = _CHUNK_SIZE) -> list[str]:
    """Split text into ~size-char chunks on paragraph then sentence boundaries.

    Packing whole paragraphs/sentences keeps each chunk readable rather than
    cutting mid-word. A single over-long sentence is hard-split as a last
    resort so no chunk exceeds size.
    """
    if not text:
        return [""]
    chunks: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        if not para:
            continue
        candidate = f"{cur}\n\n{para}" if cur else para
        if len(candidate) <= size:
            cur = candidate
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if len(para) <= size:
            cur = para
            continue
        for sentence in _re.split(r"(?<=[.!?])\s+", para):
            if not sentence:
                continue
            cand = f"{cur} {sentence}" if cur else sentence
            if len(cand) <= size:
                cur = cand
                continue
            if cur:
                chunks.append(cur)
            while len(sentence) > size:
                chunks.append(sentence[:size])
                sentence = sentence[size:]
            cur = sentence
    if cur:
        chunks.append(cur)
    return chunks or [""]


# Human act titles/abbreviations -> canonical slug, for resolving the
# legislation_refs strings stored in case summaries.
_ACT_TITLE_TO_SLUG = {
    "income tax assessment act 1997": "itaa-1997",
    "income tax assessment act 1936": "itaa-1936",
    "a new tax system (goods and services tax) act 1999": "gst-1999",
    "goods and services tax act 1999": "gst-1999",
    "fringe benefits tax assessment act 1986": "fbt-1986",
    "fringe benefits tax act 1986": "fbt-1986",
    "taxation administration act 1953": "taa-1953",
    "superannuation industry (supervision) act 1993": "sis-1993",
    "corporations act 2001": "corporations-act-2001",
    "anti-money laundering and counter-terrorism financing act 2006": "aml-ctf-2006",
    "income tax act 2007": "nz-it-2007",
    **state_tax_act_names(),
}


def _act_slug_from_title(title: str) -> str:
    """Map a human act title/abbreviation to its canonical slug ('' if unknown)."""
    t = _re.sub(r"\s*\((?:cth|commonwealth)\)", "", str(title or ""), flags=_re.IGNORECASE)
    # Drop a trailing schedule qualifier ("Sch 1," / "Schedule 2") — the schedule
    # is part of the act, not a different act.
    t = _re.sub(r"\s+sch(?:edule)?\.?\s+[0-9]+[a-z]?\s*,?\s*$", "", t, flags=_re.IGNORECASE)
    t = _re.sub(r"\s+", " ", t).strip().lower().rstrip(".,;")
    if not t:
        return ""
    if t in _ACT_TITLE_TO_SLUG:
        return _ACT_TITLE_TO_SLUG[t]
    from backend.doc_ids import ACT_SLUGS
    try:
        from backend.resolve_ref import _normalise_act_hint
        slug = _normalise_act_hint(t)
        if slug:
            return slug
    except Exception:
        pass
    try:
        from backend.services import data_loader as _dl
        slug = _dl.resolve_act_id(t)
        if slug in ACT_SLUGS:
            return slug
    except Exception:
        pass
    return ""


def _leg_ref_item(ref) -> dict | None:
    """Resolve one legislation reference (string or dict) to a link item with an ID."""
    if isinstance(ref, dict):
        act_title = str(ref.get("act_title") or ref.get("act") or "").strip()
        section = str(ref.get("section_reference") or ref.get("section") or "").strip()
        context = str(ref.get("context") or "").strip()
    else:
        raw = str(ref or "").strip()
        act_title, section, context = raw, "", ""
        m = _re.match(
            r"^(?P<act>.*?)\s+s(?:s|ection)?\.?\s*(?P<sec>[0-9][\w.\-]*)\s*$",
            raw, _re.IGNORECASE,
        )
        if m:
            act_title = m.group("act").strip()
            section = m.group("sec").strip()
        else:
            # 'Sch 1, s 284-75' style — pull the section out of the tail.
            m2 = _re.search(r"\bs(?:s|ection)?\.?\s*([0-9][\w.\-]*)\s*$", raw, _re.IGNORECASE)
            if m2 and "," in raw:
                section = m2.group(1).strip()
                act_title = raw[:m2.start()].strip().rstrip(",").strip()
    section = _re.sub(r"^(?:s(?:s|ection)?\.?\s*)", "", section, flags=_re.IGNORECASE).strip()
    slug = _act_slug_from_title(act_title)
    item = {
        "type": "legislation",
        "id": f"leg:{slug}:{section}" if (slug and section) else "",
        "citation": f"{act_title} s {section}".strip() if section else act_title,
        "title": context or (f"s {section}" if section else act_title),
        "act": slug,
        "section": section,
    }
    return item if (item["citation"] or item["id"]) else None


def _case_item_id(citation: str) -> str:
    try:
        from backend.doc_ids import case_id
        return case_id(citation)
    except Exception:
        return ""


def _case_ref_item(ref) -> dict | None:
    """Resolve one case reference (string or dict) to a link item with an ID."""
    if isinstance(ref, dict):
        cit = str(ref.get("cited_citation") or ref.get("citation") or "").strip()
        name = str(ref.get("cited_case_name") or ref.get("case_name") or "").strip()
    else:
        raw = str(ref or "").strip()
        cit, name = raw, ""
        m = _re.match(r"^(.*?)\s+[—–-]\s+(.*)$", raw)
        if m and _re.search(r"\[\d{4}\]", m.group(1)):
            cit, name = m.group(1).strip(), m.group(2).strip()
    norm = _normalise_case_citation(cit)
    item = {
        "type": "case",
        "id": _case_item_id(norm) if norm else "",
        "citation": norm or cit,
        "title": name,
    }
    return item if (item["citation"] or item["id"]) else None


def _ruling_link_item(citation: str, title: str = "") -> dict:
    """Build a unified link item for a public ruling citation."""
    from backend.doc_ids import ruling_id
    cit = str(citation or "").strip()
    item = {"type": "ruling", "id": "", "citation": cit, "title": title or ""}
    m = _re.match(r"^([A-Za-z ]+?)\s*(\d{4})/(\d+)$", cit)
    if m:
        item["id"] = ruling_id(m.group(1).strip(), int(m.group(2)), int(m.group(3)))
    return item


def _fetch_suggestions(ref: str, error: dict) -> list[str]:
    """Nearest-match suggestions for a failed fetch (spec §5/§4.3).

    resolve_ref only knows the error; the tool can probe the data to say
    *where* the document does exist — e.g. 's 8-1 ITAA 1936' -> itaa-1997.
    """
    out: list[str] = []
    existing = [s for s in (error.get("suggestions") or []) if s]
    msg = str(error.get("message") or "")
    m = _re.search(
        r"section\s+([0-9][\w.\-]*)\s+not found in\s+([a-z0-9-]+)",
        msg, _re.IGNORECASE,
    )
    if m:
        section, asked_act = m.group(1), m.group(2)
        from backend.doc_ids import leg_id as _leg_id
        for act in (a["id"] for a in _public_act_entries()):
            if act == asked_act or _mcp_denied(_mcp_act_key(act)):
                continue
            if section in _get_act_section_ids(act):
                out.append(
                    f"{_leg_id(act, section)} — s {section} exists in {act}."
                )
    if not out and not existing:
        try:
            hits = fts_search(ref.strip(), None, limit=3).get("results", [])
            for h in hits:
                act, sec = h.get("act", ""), h.get("section", "")
                # Gate like search/outline: no titles from acts the caller can't read.
                if act and sec and act != "tax-cases" and not _mcp_denied(_mcp_act_key(act)):
                    out.append(f"leg:{act}:{sec} — {h.get('title', '')}".strip())
        except Exception:
            pass
    merged: list[str] = []
    for s in existing + out:
        if s and s not in merged:
            merged.append(s)
    return merged


# ---------------------------------------------------------------------------
# Auth middleware — applied to the streamable_http_app in main.py
# ---------------------------------------------------------------------------

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.requests import Request
import collections

# OAuth token validation
from backend.oauth_provider import provider as oauth_provider
from backend.auth import AUTH_DISABLED


_req_usage: ContextVar = ContextVar("mcp_req_usage", default=None)


def _usage_blob() -> dict:
    """Usage info for the current request (owner/tier set by the middleware)."""
    u = _req_usage.get()
    if not u:
        return {}
    usage = token_manager.daily_usage(u["owner"], u["tier"])
    return {"used": usage["used"], "cap": usage["cap"], "reset_in": usage["reset_in"]}


def _append_usage(result):
    """Append a _usage footer object to a tool's JSON-string result."""
    blob = _usage_blob()
    if not blob:
        return result
    if isinstance(result, str):
        try:
            obj = json.loads(result)
            if isinstance(obj, dict):
                obj["_usage"] = blob
                return json.dumps(obj, indent=2)
        except Exception:
            pass
    return result


def _source_notice(result: dict) -> str | None:
    """ONE compact source/attribution line for a document-level tool result.

    The web app shows the same string as the collapsed notice line. MCP deliberately carries
    NO notice on search/list rows or per row — only on the document the agent actually reads,
    which keeps attribution where the content is served without taxing every response.
    Full wording is available through the web app's notice; the compact line names the source.
    """
    try:
        from backend.services.data_loader import licence_notice, document_licence
        t = (result.get("type") or "").lower()
        if t in ("legislation", "section") and result.get("act"):
            lic = licence_notice(result["act"])
        elif t in ("ruling", "private_ruling", "private-ruling"):
            lic = document_licence("private-rulings" if t.startswith("private")
                                   else "rulings", source_url=result.get("ato_url") or None)
        elif t == "rg":
            lic = document_licence("regulatory-guides", source_url=result.get("page_url") or None)
        elif t == "case":
            lic = document_licence("tax-cases", result.get("citation") or result.get("id") or "")
        else:  # treaty, afsa guidance, commentary: no notice by design
            lic = None
    except Exception:  # a missing/1st-run record must never break a fetch
        return None
    if not lic:
        return None
    line = lic.get("notice_line")
    if not line:
        return None
    badge = lic.get("notice_badge")
    return f"{badge} · {line}" if badge else line


def _with_footer(fn):
    """Wrap a tool so its JSON result carries the usage footer."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return _append_usage(await fn(*args, **kwargs))
    return wrapper


def _set_rate_limit_headers(response: Response, owner: str, tier: str) -> Response:
    """Set X-RateLimit-* headers on the response."""
    usage = token_manager.daily_usage(owner, tier)
    cap = usage.get("cap")
    used = usage.get("used", 0)
    response.headers["X-RateLimit-Limit"] = str(cap) if cap is not None else "unlimited"
    response.headers["X-RateLimit-Remaining"] = str(max(0, cap - used)) if cap is not None else "unlimited"
    response.headers["X-RateLimit-Reset"] = str(usage.get("reset_in", 0))
    return response


# ── Failed MCP auth limiter ────────────────────────────────────────────────
# Keyed by client IP. Because the HTTP rate limiter buckets /mcp/<guess> by
# full path, each token guess previously got a fresh bucket and was never
# limited; this catches repeated failures regardless of the path/header used.
_FAILED_AUTH_WINDOW = 600   # 10 minutes
_FAILED_AUTH_MAX = 20       # failures per IP per window
_failed_auth: dict[str, collections.deque] = {}


def _client_ip(request: Request) -> str:
    ip = request.headers.get("cf-connecting-ip")
    if not ip:
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
        ip = ip.split(",")[0].strip()
    return ip


def _failed_auth_limited(ip: str) -> bool:
    dq = _failed_auth.get(ip)
    if not dq:
        return False
    cutoff = time.time() - _FAILED_AUTH_WINDOW
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq) >= _FAILED_AUTH_MAX


def _record_failed_auth(ip: str) -> None:
    dq = _failed_auth.setdefault(ip, collections.deque())
    cutoff = time.time() - _FAILED_AUTH_WINDOW
    while dq and dq[0] < cutoff:
        dq.popleft()
    dq.append(time.time())


class MCPAuthMiddleware(BaseHTTPMiddleware):
    """Token auth + rate limiting for MCP endpoints.

    Accepts token from:
      - Authorization: Bearer ***
      - X-API-Key: *** (alternative to Bearer, for Cloudflare WAF bypass)
      - ?token=<token> query param
      - /mcp/<token>  path segment (bypasses Cloudflare WAF)
    Skips auth when DEV_MODE=true on a non-production instance (auth.AUTH_DISABLED).
    """

    async def dispatch(self, request: Request, call_next):
        # One usage row per request, written in `finally` so rejections are logged too.
        ev = usage_log.new_event("mcp", client_ip=_client_ip(request))
        ctx = usage_log.current.set(ev)
        start = time.perf_counter()
        try:
            return await self._dispatch(request, call_next, ev)
        except BaseException as e:
            if ev.get("status") != "error":
                ev.update(status="error", error_kind=error_label(e), error_detail=str(e)[:1000])
            raise
        finally:
            usage_log.current.reset(ctx)
            ev["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
            usage_log.record(ev)

    async def _dispatch(self, request: Request, call_next, ev: dict):
        start = time.perf_counter()
        request_id = ev["request_id"]
        owner = ""
        tier = ""

        # ── Protocol-version compatibility ────────────────────────────────────────
        # A newer MCP client sends the revision it wanted (e.g. 2026-07-28) in the
        # MCP-Protocol-Version header on EVERY request. The installed SDK only knows its own
        # list, and answers -32600 "Unsupported protocol version", which kills the session
        # outright — 23 such failures in the log, 16 of them one paying user in a single day.
        # Down-negotiate an unknown revision to the newest we do speak instead of refusing.
        # Strictly the transport spec says reject; interop with a customer's client wins, and
        # the downgrade is recorded so it stays visible.
        # An empty header counts as unknown: 2.x routes it to the modern path, which rejects it.
        _hv = request.headers.get("mcp-protocol-version")
        if _hv is not None and _hv not in _accepted_versions():
            request.scope["headers"] = [
                (k, v) for k, v in request.scope["headers"]
                if k.lower() != b"mcp-protocol-version"
            ] + [(b"mcp-protocol-version", _LATEST_HANDSHAKE.encode())]
            logging.getLogger("lawkitty.mcp").warning(
                "mcp_protocol_downgrade",
                extra={"extra_info": {"requested": _hv, "used": _LATEST_HANDSHAKE,
                                      "request_id": request_id}},
            )
            ev.setdefault("params", json.dumps(
                {"protocol_version_requested": _hv, "used": _LATEST_HANDSHAKE})[:500])

        # Read the body once (Starlette caches it, so downstream still gets it).
        # Used for body-based auth and for structured per-call logging.
        try:
            body_bytes = await request.body()
        except Exception:
            body_bytes = b""
        method = ""
        tool = ""
        body_params: dict = {}
        _b = None
        try:
            if body_bytes:
                _b = json.loads(body_bytes)
                method = _b.get("method", "") or ""
                _p = _b.get("params", {})
                if isinstance(_p, dict):
                    body_params = _p
                    tool = _p.get("name", "") or ""
        except Exception:
            pass
        if _b is not None:
            compat = _compat_body(body_bytes, _b)
            if compat is not None:
                # Starlette replays request._body to the app; keep Content-Length consistent.
                request._body = compat
                request.scope["headers"] = [
                    (k, v) for k, v in request.scope["headers"] if k.lower() != b"content-length"
                ] + [(b"content-length", str(len(compat)).encode())]
        if method == "tools/call" and tool:
            ev.update(usage_log.describe_call(tool, body_params.get("arguments") or {}))
        else:
            # Protocol traffic (initialize, tools/list, ...) — counted by the daily cap, so logged.
            ev.update(tool=f"rpc:{method or request.method}", content_type="other")

        def _emit(status: int, error_code: str = "") -> None:
            logging.getLogger("lawkitty.mcp").info(
                "mcp_call",
                extra={"extra_info": {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "request_id": request_id,
                    "owner": owner,
                    "tier": tier,
                    "method": method,
                    "tool": tool,
                    "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                    "http_status": status,
                    "error_code": error_code,
                }},
            )

        def _reject(kind: str, detail: str) -> None:
            ev.update(status="error", error_kind=kind, error_detail=detail, user_email=owner or None)

        # Same fail-closed switch as the web app: DEV_MODE is ignored on a production instance.
        if AUTH_DISABLED:
            # Act as the same synthetic user the web app uses, so MCP sees what the UI shows.
            from backend.auth import _synthetic_user
            ev["user_email"] = _synthetic_user()["email"]
            return await self._forward(request, call_next, ev, method)

        client_ip = ev["client_ip"]
        if _failed_auth_limited(client_ip):
            _emit(429, "rate_limited")
            _reject("auth_rate_limited", "Too many failed authentication attempts")
            return Response(
                "Too many failed authentication attempts",
                status_code=429,
                headers={"Retry-After": str(_FAILED_AUTH_WINDOW)},
            )

        token = ""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        if not token:
            token = request.headers.get("X-API-Key", "")
        if not token:
            token = request.query_params.get("token", "")
        if not token:
            token = request.query_params.get("v", "")
        if not token:
            token = request.query_params.get("_auth", "")
        if not token:
            token = request.query_params.get("x", "")
        # Path-segment token: /mcp/<token>
        if not token:
            path = request.url.path
            if path.startswith("/mcp/") and len(path) > 5:
                token = path.split("/mcp/", 1)[-1].split("?")[0].split("/")[0]

        # Body-based auth (JSON-RPC params._auth) — bypasses Cloudflare WAF
        if not token:
            token = body_params.get("_auth", "") or body_params.get("token", "")

        # Cookie-based auth — Cloudflare doesn't inspect cookies as credentials
        if not token:
            token = request.cookies.get("token", "")

        # Custom header auth — X-Session-Id bypasses Cloudflare DDoS
        if not token:
            token = request.headers.get("X-Session-Id", "")

        if not token:
            _record_failed_auth(client_ip)
            _emit(401, "missing_token")
            _reject("auth_missing", "Missing token")
            # resource_metadata tells a client holding only the link where to sign in (RFC 9728).
            from backend.oauth_provider import RESOURCE_METADATA_URL
            return Response("Missing token", status_code=401,
                            headers={"WWW-Authenticate": f'Bearer resource_metadata="{RESOURCE_METADATA_URL}"'})

        # Resolve the token's owner (gating and usage are per owner).
        owner = token_manager.owner_of(token)
        if owner is not None:
            tier = token_manager.tier_for_email(owner)
            # Store the hash, never the bearer itself: the event is copied, re-read by every
            # tool wrapper and could be dumped by any logger or debug endpoint.
            ev["token"] = hashlib.sha256(token.encode()).hexdigest()
            ev["token_hashed"] = True
        else:
            # Fallback: try static MCP_AUTH_TOKEN from env (operator token — unlimited)
            mcp_auth_token = os.environ.get("MCP_AUTH_TOKEN", "") or os.environ.get("LEGISLATION_BEARER_TOKEN", "")
            if not isinstance(token, str):
                token = ""
            if mcp_auth_token and hmac.compare_digest(token.encode("utf-8", "ignore"), mcp_auth_token.encode()):
                owner, tier = "operator", "power"
                ev[_OPERATOR_FIELD] = _OPERATOR  # the one unrestricted HTTP caller
            else:
                # Fallback: try OAuth access token
                oauth_data = oauth_provider.load_access_token(token)
                if not oauth_data:
                    _record_failed_auth(client_ip)
                    _emit(403, "invalid_token")
                    _reject("auth_invalid", "Invalid or revoked token")
                    return Response("Invalid or revoked token", status_code=403)
                owner = oauth_data["user_email"]
                tier = token_manager.tier_for_email(owner)
        ev["user_email"] = owner

        allowed, reason = token_manager.check_rate_limit(token, owner=owner, tier=tier)
        if reason == "upgrade_required":
            # Token owner has no users row (tier "anonymous") — no MCP access.
            _emit(403, "no_account")
            _reject("no_account", "Token owner has no account (tier has no MCP access)")
            return Response(json.dumps({"error": "no_account"}), status_code=403, media_type="application/json")
        if not allowed:
            retry_after = "60"
            if reason.startswith("Daily MCP limit reached"):
                retry_after = str(int(86400 - (time.time() % 86400)))
            _emit(429, "rate_limited")
            _reject("daily_cap" if reason.startswith("Daily MCP limit reached") else "rate_limited", reason)
            return Response(reason, status_code=429, headers={"Retry-After": retry_after})

        tok = _req_usage.set({"owner": owner, "tier": tier})
        try:
            response = await self._forward(request, call_next, ev, method)
        finally:
            _req_usage.reset(tok)
        if method == "tools/call":
            response = _set_rate_limit_headers(response, owner, tier)
        _emit(response.status_code)
        return response

    async def _forward(self, request: Request, call_next, ev: dict, method: str):
        """Run the MCP app; record response size and any error the tool wrapper didn't see
        (unknown tool, argument validation, JSON-RPC errors)."""
        if method == "server/discover" and not MCP_MODERN_ERA:
            # Newer clients probe with server/discover before initialize. While the modern era
            # is off, answer -32601 Method not found ourselves, exactly as under mcp 1.x: the
            # spec's signal for a legacy server, on which clients fall back to initialize.
            # With the flag on the SDK answers natively (a DiscoverResult for an enveloped
            # 2026-07-28 probe, -32601 for a legacy one).
            try:
                rpc_id = json.loads(await request.body()).get("id")
            except Exception:
                rpc_id = None
            ev.update(status="ok", response_bytes=None)
            return Response(
                json.dumps({"jsonrpc": "2.0", "id": rpc_id,
                            "error": {"code": -32601, "message": "Method not found: server/discover"}}),
                status_code=200, media_type="application/json")
        response = await call_next(request)
        if "json" not in (response.headers.get("content-type") or ""):
            cl = response.headers.get("content-length")
            ev["response_bytes"] = int(cl) if cl and cl.isdigit() else None
            if response.status_code >= 400 and ev.get("status") != "error":
                ev.update(status="error", error_kind=f"http_{response.status_code}")
            ev.setdefault("status", "ok")
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        ev["response_bytes"] = len(body)
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            err = payload.get("error")
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            if err and ev.get("status") != "error":
                ev.update(status="error", error_kind=f"jsonrpc_{err.get('code')}" if isinstance(err, dict) else "jsonrpc",
                          error_detail=str(err.get("message") if isinstance(err, dict) else err)[:1000])
            elif result.get("isError") and ev.get("status") != "error":
                text = " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
                # The wrapper didn't run (unknown tool / bad arguments) or didn't flag it.
                kind = "ToolError" if ev.get("_tool_ran") or "Unknown tool" not in text else "UnknownTool"
                if not ev.get("_tool_ran") and "validation error" in text.lower():
                    kind = "ValidationError"
                ev.update(status="error", error_kind=kind, error_detail=text[:1000])
        if response.status_code >= 400 and ev.get("status") != "error":
            ev.update(status="error", error_kind=f"http_{response.status_code}")
        ev.setdefault("status", "ok")
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        if "content-type" not in {k.lower() for k in headers}:
            # Without this the rebuilt Response defaults to text/plain and JSON-RPC clients that
            # key off the media type fall back to raw text.
            headers["content-type"] = response.media_type or "application/json"
        # background= keeps the MCP app's post-response task alive; dropping it silently
        # stopped usage logging.
        return Response(content=body, status_code=response.status_code, headers=headers,
                        background=getattr(response, "background", None))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_ALIAS_PATTERNS: list[tuple[_re.Pattern, str, str | None, str, str | None, str | None]] = [
    # (compiled_regex, act_id, resolved_ref, display_act, description, url_suffix)
    # Order matters: more specific patterns first.

    # --- ITAA 1936 well-known aliases ---
    (
        _re.compile(r'^(?:s(?:ec(?:tion)?)?\.?\s*)?(?:div(?:ision)?\.?\s*)?7[AEae](?:\s|$)', _re.IGNORECASE),
        "itaa-1936",
        "Division 7A",
        "ITAA 1936",
        "Division 7A — Private company dividends (ss 109Y–109ZQ)",
        "itaa-1936#Division_7A",
    ),
    (
        _re.compile(r'^(?:s(?:ec(?:tion)?)?\.?\s*)?100[AEae](?:\s|$)', _re.IGNORECASE),
        "itaa-1936",
        "100A",
        "ITAA 1936",
        "s 100A — Reimbursement agreements",
        "itaa-1936#100A",
    ),
    (
        _re.compile(r'^(?:s(?:ec(?:tion)?)?\.?\s*)?Part IV[AEae](?:\s|$)', _re.IGNORECASE),
        "itaa-1936",
        "Part IVA",
        "ITAA 1936",
        "Part IVA — General anti-avoidance rules (ss 177A–177P)",
        "itaa-1936#Part_IVA",
    ),
    (
        _re.compile(r'^(?:s(?:ec(?:tion)?)?\.?\s*)?109[YZ](?:\s|$)', _re.IGNORECASE),
        "itaa-1936",
        None,  # will resolve specific section via exact match fallback
        "ITAA 1936",
        None,
        None,
    ),
    # --- ITAA 1997 well-known aliases ---
    (
        _re.compile(r'^(?:s(?:ec(?:tion)?)?\.?\s*)?(?:Subdiv(?:ision)?\.?\s*)?115-C(?:\s|$)', _re.IGNORECASE),
        "itaa-1997",
        "Subdivision 115-C",
        "ITAA 1997",
        "Subdivision 115-C — Net capital gain (ss 115-215 to 115-228)",
        "itaa-1997#Subdivision_115-C",
    ),
    (
        _re.compile(r'^(?:s(?:ec(?:tion)?)?\.?\s*)?(?:Div(?:ision)?\.?\s*)?152(?:\s|$)', _re.IGNORECASE),
        "itaa-1997",
        "Division 152",
        "ITAA 1997",
        "Division 152 — Small business relief (ss 152-1 to 152-430)",
        "itaa-1997#Division_152",
    ),
]


# ── Hyphenated section routing helpers ─────────────────────────────

@functools.lru_cache(maxsize=8)
def _get_act_section_ids(act: str) -> set[str]:
    """Return a set of all section, division, and subdivision IDs in an act."""
    from backend.services.data_loader import load_tree
    try:
        tree = load_tree(act)
    except Exception:
        return set()
    ids: set[str] = set()
    for part in tree.get("parts", []):
        for sec in part.get("sections", []):
            ids.add(sec.get("id", ""))
        for div in part.get("divisions", []):
            ids.add(div.get("id", ""))  # collect division IDs too (e.g. "Division 7A")
            for sec in div.get("sections", []):
                ids.add(sec.get("id", ""))
            for sub in div.get("subdivisions", []):
                ids.add(sub.get("id", ""))  # collect subdivision IDs too
                for sec in sub.get("sections", []):
                    ids.add(sec.get("id", ""))
    return ids


def _resolve_hyphenated_act(bare: str) -> str:
    """Probe candidate acts to find which one contains the given hyphenated section.
    Prefers non-ITAA1997 acts when a section exists in multiple (e.g. 195-1 is in
    both itaa-1997 and gst-1999 — the GST Act is the correct one)."""
    candidates = ["gst-1999", "taa-1953", "itaa-1997"]
    found = [act for act in candidates if bare in _get_act_section_ids(act)]
    if not found:
        return "itaa-1997"  # fallback
    # If found in only one act, use it
    if len(found) == 1:
        return found[0]
    # If found in multiple, prefer the first non-itaa-1997 hit
    for act in found:
        if act != "itaa-1997":
            return act
    return "itaa-1997"


def _section_exists_in_act(act: str, section_id: str) -> bool:
    """Check if a section ID exists in the given act's tree via section-index lookup.

    Returns True if the section exists in the act, False otherwise.
    Used to prevent pattern-based alias resolution from returning false positives
    for non-existent references.
    
    Normalizes 'Division X' → bare 'X' to match tree IDs.
    """
    if not section_id or not act:
        return False
    ids = _get_act_section_ids(act)
    if section_id in ids:
        return True
    # Try normalized form: "Division 7A" → "7A", "Part IVA" → "IVA"
    for prefix in ("Division ", "Part ", "Subdivision "):
        if section_id.startswith(prefix):
            normalized = section_id[len(prefix):]
            if normalized in ids:
                return True
    return False


def _section_title(act: str, section_id: str) -> str | None:
    """Human title of a section/division/subdivision in an act, or None.

    resolve_alias previously returned a routing description ("s 8-1 —
    hyphenated section number routed to ITAA 1997") instead of the real
    heading; callers want "General deductions". Looks the id up in the act
    tree (titles are already normalised there).
    """
    if not act or not section_id:
        return None
    bare = section_id
    for prefix in ("Division ", "Part ", "Subdivision "):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
            break
    try:
        tree = load_tree(act)
    except Exception:
        return None
    for part in tree.get("parts", []):
        if part.get("id") == bare and part.get("title"):
            return part["title"]
        for sec in part.get("sections", []):
            if sec.get("id") == bare:
                return sec.get("title") or None
        for div in part.get("divisions", []):
            if div.get("id") == bare and div.get("title"):
                return div["title"]
            for sec in div.get("sections", []):
                if sec.get("id") == bare:
                    return sec.get("title") or None
            for sub in div.get("subdivisions", []):
                if sub.get("id") == bare and sub.get("title"):
                    return sub["title"]
                for sec in sub.get("sections", []):
                    if sec.get("id") == bare:
                        return sec.get("title") or None
    return None


@mcp.tool(structured_output=False)
async def resolve_alias(reference: str) -> str:
    """Resolve a section alias or short-hand reference to its act and section number.

    Handles common shorthand: 's 100A', 'Div 7A', 'Part IVA', 'Subdiv 115-C',
    '109Y', '8-1', 'Div 152', etc.

    Rules:
    - Div 7A          → ITAA 1936, Division 7A (ss 109Y-109ZQ)
    - s 100A          → ITAA 1936, s 100A
    - Part IVA        → ITAA 1936, Part IVA (ss 177A-177P)
    - Subdiv 115-C    → ITAA 1997, Subdivision 115-C (ss 115-215 to 115-228)
    - Simple section numbers like 109Y → ITAA 1936, s 109Y
    - Section-number-shaped like 8-1   → ITAA 1997, s 8-1
    - Div 152         → ITAA 1997, Division 152

    Falls back to search_legislation when no rule matches.
    """
    ref = reference.strip()
    if not ref:
        return json.dumps({"error": "Empty reference provided.", "hint": _GET_INFO_HINT})

    # -- Step 1: Check well-known patterns --
    for pattern, act, resolved_ref, display_act, description, url_suffix in _ALIAS_PATTERNS:
        if pattern.match(ref):
            if resolved_ref is None and description is None:
                # Partial match that needs fallback but act is known
                # e.g. 109* — resolve via search
                break
            # Validate the resolved section actually exists in the act's tree
            if resolved_ref is None or not _section_exists_in_act(act, resolved_ref):
                # Section doesn't exist — fall through to search
                break
            return json.dumps({
                "reference": ref,
                "act": act,
                "act_display": display_act,
                "section": resolved_ref,
                "description": description,
                "url": f"{_APP_BASE}/{url_suffix}" if url_suffix else None,
                "resolved_by": "exact_rule",
            }, indent=2)

    # -- Step 2: Try regex-based routing for common patterns not in the well-known list --

    # 'Div X' or 'Division X' → ITAA 1997 (by default, but could be either)
    m = _re.match(r'^(?:div(?:ision)?\.?\s*)(\S+)$', ref, _re.IGNORECASE)
    if m:
        div = m.group(1)
        return json.dumps({
            "reference": ref,
            "act": "itaa-1997",
            "act_display": "ITAA 1997",
            "section": f"Division {div}",
            "description": f"Division {div} — resolved from bare division reference (defaulting to ITAA 1997)",
            "url": f"{_APP_BASE}/itaa-1997#Division_{div}",
            "resolved_by": "division_pattern",
            "note": "Division references are assumed ITAA 1997 by default. If you need ITAA 1936, use 's 109Y' format instead.",
        }, indent=2)

    # 'Part X' or 'Part X-Y' → ITAA 1936
    m = _re.match(r'^(?:Part\s+)([A-Za-z0-9]+(?:[-][A-Za-z0-9]+)?)$', ref, _re.IGNORECASE)
    if m:
        part = m.group(1)
        act_id = "itaa-1936"
        act_display = "ITAA 1936"
        # If it has a hyphen (e.g. "Part 3-1"), it's ITAA 1997
        if '-' in part:
            act_id = "itaa-1997"
            act_display = "ITAA 1997"
        return json.dumps({
            "reference": ref,
            "act": act_id,
            "act_display": act_display,
            "section": f"Part {part}",
            "description": f"Part {part} — resolved from bare Part reference",
            "url": f"{_APP_BASE}/{act_id}#Part_{part}",
            "resolved_by": "part_pattern",
        }, indent=2)

    # 'Subdiv X' or 'Subdivision X' → ITAA 1997
    m = _re.match(r'^(?:subdiv(?:ision)?\.?\s+)(\S+)$', ref, _re.IGNORECASE)
    if m:
        subdiv = m.group(1)
        return json.dumps({
            "reference": ref,
            "act": "itaa-1997",
            "act_display": "ITAA 1997",
            "section": f"Subdivision {subdiv}",
            "description": f"Subdivision {subdiv} — resolved from bare subdivision reference",
            "url": f"{_APP_BASE}/itaa-1997#Subdivision_{subdiv}",
            "resolved_by": "subdivision_pattern",
        }, indent=2)

    # Strip leading 's', 'sec', 'section' prefix
    bare = _re.sub(r'^(?:s(?:ec(?:tion)?)?\.?\s+)', '', ref, flags=_re.IGNORECASE).strip()

    # Section-number-shaped with hyphen → probe across candidate acts
    if _re.match(r'^\d+[-]\d+[A-Za-z0-9]*$', bare):
        act_id = _resolve_hyphenated_act(bare)
        act_display = {
            "itaa-1997": "ITAA 1997", "gst-1999": "GST Act", "taa-1953": "TAA 1953"
        }.get(act_id, "ITAA 1997")
        # Validate the section actually exists in the resolved act's tree
        if _section_exists_in_act(act_id, bare):
            title = _section_title(act_id, bare)
            return json.dumps({
                "reference": ref,
                "act": act_id,
                "act_display": act_display,
                "section": bare,
                "title": title or "",
                "description": title or f"s {bare}",
                "routing": f"s {bare} — hyphenated section number routed to {act_display}",
                "url": f"{_APP_BASE}/get_section?act={act_id}&section={bare}",
                "resolved_by": "hyphenated_section_pattern",
            }, indent=2)

    # Unhyphenated alphanumeric section → ITAA 1936 style (e.g. '109Y', '100A', '23AH')
    if _re.match(r'^[A-Za-z0-9]+$', bare):
        # Validate the section actually exists in ITAA 1936 before returning
        if _section_exists_in_act("itaa-1936", bare):
            title = _section_title("itaa-1936", bare)
            return json.dumps({
                "reference": ref,
                "act": "itaa-1936",
                "act_display": "ITAA 1936",
                "section": bare,
                "title": title or "",
                "description": title or f"s {bare}",
                "routing": f"s {bare} — unhyphenated section routed to ITAA 1936",
                "url": f"{_APP_BASE}/get_section?act=itaa-1936&section={bare}",
                "resolved_by": "unhyphenated_section_pattern",
            }, indent=2)

    # -- Step 3: Entity alias map (21,548 LLM-mapped refs, e.g. "FBTAA section 49",
    #    "Glenn v Federal Commissioner of Land Tax") — verified against graph.db --
    try:
        key = alias_lookup(ref)
        if key and node_allowed(_mcp_avail(), key):
            return json.dumps({
                "reference": ref,
                "graph_key": key,
                "resolved_by": "entity_alias_map",
                "note": "Resolved via entity alias map (pipeline.entity_backstop). "
                        "Use graph_neighbourhood for the node's context block.",
            }, indent=2)
    except Exception:
        pass

    # -- Step 3b: Citation resolver (the one fetch uses): "s 8-1 ITAA 1997" names the act
    #    explicitly, so it must not fall through to a fuzzy full-text match --
    try:
        from backend.resolve_ref import resolve_ref
        hit = resolve_ref(ref)
        _act, _sec = hit.get("act"), hit.get("section")
        if hit.get("type") == "legislation" and _act and _sec and not _mcp_denied(_mcp_act_key(_act)):
            return json.dumps({
                "reference": ref,
                "act": _act,
                "act_display": _act,
                "section": _sec,
                "description": hit.get("title") or "",
                "id": hit.get("id"),
                "url": f"{_APP_BASE}/get_section?act={_act}&section={_sec}",
                "resolved_by": "citation",
            }, indent=2)
    except Exception:
        pass

    # -- Step 4: Fallback to full-text search --
    try:
        result = fts_search(ref, None, limit=5)
        hits = [h for h in result.get("results", []) if h.get("act") not in _INTERNAL_COLLECTIONS
                and not _mcp_denied(_mcp_act_key(h.get("act") or ""))]
        if hits:
            best = hits[0]
            return json.dumps({
                "reference": ref,
                "act": best.get("act", ""),
                "act_display": best.get("act", ""),
                "section": best.get("section", ""),
                "description": best.get("title", ""),
                "url": f"{_APP_BASE}/get_section?act={best.get('act', '')}&section={best.get('section', '')}",
                "resolved_by": "search_fallback",
                "search_results": [
                    {
                        "act": h.get("act", ""),
                        "section": h.get("section", ""),
                        "title": h.get("title", ""),
                    }
                    for h in hits[:3]
                ],
            }, indent=2)
    except Exception:
        pass

    return json.dumps({
        "reference": ref,
        "error": f"Could not resolve reference '{ref}'. Try search_legislation or a more specific format.",
        "hint": _GET_INFO_HINT,
    }, indent=2)


_GRAPH_DB = DATA_DIR / "graph.db"


def _graph_edges_to(act: str, section: str, edge_type: str, node_type: str, limit: int = 10) -> list[tuple[str, str, str]]:
    """Graph neighbours of a section node: (key, label, meta_json).

    Shared by cases/rulings/commentary lookups — single read-only connection,
    exact key match only (graph keys are canonical).
    """
    if not _GRAPH_DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{_GRAPH_DB}?mode=ro", uri=True, timeout=10)
        try:
            row = conn.execute(
                "SELECT id FROM nodes WHERE key=?", (f"section:{act}:{section}",)
            ).fetchone()
            if row is None:
                return []
            cid = row[0]
            rows = conn.execute(
                """
                SELECT n.key AS nkey, n.label AS nlabel, n.meta AS nmeta
                FROM graph_edges e
                JOIN nodes n ON n.id = CASE WHEN e.source_id=? THEN e.target_id ELSE e.source_id END
                WHERE (e.source_id=? OR e.target_id=?)
                  AND e.edge_type=? AND n.node_type=?
                GROUP BY n.id
                ORDER BY e.weight DESC
                LIMIT ?
                """,
                (cid, cid, cid, edge_type, node_type, limit),
            ).fetchall()
            return [(r[0], r[1], r[2]) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def _graph_cases_for_section(act: str, section: str, limit: int = 10) -> list[dict]:
    """Cases linked to a section via graph `considered_in` edges."""
    cases = _load_cases_map()
    out = []
    for key, label, meta_json in _graph_edges_to(act, section, "considered_in", "case", limit):
        citation = key.split(":", 1)[1] if ":" in key else key
        meta = {}
        try:
            meta = json.loads(meta_json or "{}")
        except Exception:
            pass
        entry = cases.get(citation, {})
        out.append({
            "type": "case",
            "citation": citation,
            "title": entry.get("title") or label,
            "short_name": entry.get("short_name") or "",
            "court": meta.get("court") or entry.get("court") or "",
        })
    return out


def _graph_rulings_for_section(act: str, section: str, limit: int = 10) -> list[dict]:
    """Public rulings linked to a section via graph `interpreted_by` edges.

    `applies` edges to public rulings are parallel duplicates of
    `interpreted_by` (provenance-verified), so they're not double-counted.
    Private rulings are returned separately via `applies` → private_ruling.
    """
    rulings = _load_rulings_map()
    out = []
    for key, label, _meta in _graph_edges_to(act, section, "interpreted_by", "public_ruling", limit):
        citation = key.split(":", 1)[1] if ":" in key else key
        title = ""
        # Ruling file stems use several conventions: TR_2014_5 (space→_), PSLA_2007_20 (space dropped), ATOID_2012_3.
        candidates = {
            citation,
            citation.replace("ATOID ", "AID "),
            citation.replace("ATOID_", "AID_", 1),
            citation.replace(" ", "_").replace("/", "_").replace("-", "_"),
            citation.replace(" ", "").replace("/", "_").replace("-", "_"),
        }
        r = None
        title = ""
        for cand in candidates:
            r = rulings.get(cand)
            if r:
                title = r.get("full_title") or r.get("title") or ""
                break
        dl_citation = _re.sub(r"[\s/]+", "_", citation).strip("_")
        out.append({
            "type": "ruling",
            "citation": citation,
            "title": title or label,
            "ato_url": r.get("ato_url", "") if r else "",
            "austlii_url": r.get("austlii_url", "") if r else "",
            "download_url": _abs(f"/api/ruling/{dl_citation}/download"),
        })
    return out


def _graph_private_rulings_for_section(act: str, section: str, limit: int = 10) -> list[dict]:
    """Private rulings linked to a section via graph `applies` edges."""
    out = []
    for key, label, _meta in _graph_edges_to(act, section, "applies", "private_ruling", limit):
        auth = key.rsplit("/", 1)[-1]
        out.append({
            "type": "private_ruling",
            "auth_number": auth,
            "label": label if label.startswith("EV/") else f"EV/{auth}",
            "url": f"/private-rulings/{auth}",
            "ato_url": (f"https://www.ato.gov.au/law/view/print"
                        f"?DocID=EV/{auth}&PiT=99991231235958"),
            "download_url": _abs(f"/api/private-ruling/{auth}/download"),
        })
    return out


@functools.lru_cache(maxsize=1)
def _load_cases_map() -> dict[str, dict]:
    # Name lookup uses the wide index (CASE_DIR + scripts/cleaned/summaries);
    # the /api/cases listing still uses load_cases() (CASE_DIR only).
    from backend.services.data_loader import load_case_name_index
    return load_case_name_index()


@functools.lru_cache(maxsize=1)
def _load_rulings_map() -> dict[str, dict]:
    from backend.services.data_loader import load_rulings
    out: dict[str, dict] = {}
    for r in load_rulings():
        out[r.get("citation", "")] = r
        out[r.get("citation", "").replace("ATOID_", "AID_", 1)] = r
    return out




async def list_acts() -> str:
    """List all available acts and ATO rulings.

    Backed by _public_act_entries() — the same source get_info() uses — so the
    internal display-spec collection is never exposed and compilation dates are
    ISO 8601.
    """
    acts = list(_public_act_entries())
    acts.append({"id": "rulings", "name": "Public Rulings"})
    avail = _mcp_avail()
    acts = [a for a in acts if act_key(a["id"]) in avail]
    return json.dumps({"acts": acts}, indent=2)


async def get_act_tree(act: str, depth: str = "sections", part: str | None = None,
                       offset: int = 0) -> str:
    """Get the structure of an act (parts, divisions, sections).

    depth: 'parts' returns only parts (fast), 'divisions' includes divisions,
           'sections' (default) includes all sections.

    part: Optional part ID to scope the result to one part (e.g. '1-1').
          Required for large acts at depth='sections' — the full ITAA 1997
          tree is ~1MB and burns tokens. When part is given, only that part's
          sections are returned.

    offset: Section offset within the current scope (whole act, or the part
            when part= is set). Pages at 400 sections; pass the previous
            response's offset + sections_returned (or the next_offset hint)
            to read further. Largest parts exceed 400 sections (ITAA 1997
            Part 4-5 = 489, ITAA 1936 Part III = 471) — offset is the only
            way to reach their tail sections.
    """
    if _mcp_hidden_denied(act):  # answer exactly as for an act this instance lacks
        return (await get_act_tree(_UNKNOWN_ACT, depth, part, offset)).replace(_UNKNOWN_ACT, act)
    if (_denied := _mcp_denied(_mcp_act_key(act))):
        return _denied
    try:
        tree = load_tree(act)
    except Exception:
        # load_tree raises for unknown acts — return a clean error with hint
        return json.dumps({
            "error": f"Act '{act}' not found.",
            "hint": _GET_INFO_HINT,
            "format_hint": "Valid act ids via outline (e.g. itaa-1997, itaa-1936, gst-1999, taa-1953).",
        }, indent=2)

    def _section_rows(parts: list[dict]) -> list[tuple]:
        """Flatten (part, division, subdivision, section) rows in tree order."""
        rows = []
        for p in parts:
            pid, ptitle = p.get("id"), p.get("title")
            for s in p.get("sections", []):
                rows.append((pid, ptitle, None, None, None, None, s.get("id"), s.get("title")))
            for d in p.get("divisions", []):
                did, dtitle = d.get("id"), d.get("title")
                for s in d.get("sections", []):
                    rows.append((pid, ptitle, did, dtitle, None, None, s.get("id"), s.get("title")))
                for sub in d.get("subdivisions", []):
                    sid, stitle = sub.get("id"), sub.get("title")
                    for s in sub.get("sections", []):
                        rows.append((pid, ptitle, did, dtitle, sid, stitle, s.get("id"), s.get("title")))
        return rows

    def _rebuild(rows: list[tuple]) -> list[dict]:
        """Reconstruct pruned part/division/subdivision hierarchy from a row slice."""
        from backend.doc_ids import leg_id as _leg_id
        parts: dict = {}
        order: list[str] = []
        for pid, ptitle, did, dtitle, sid, stitle, secid, sectitle in rows:
            if pid not in parts:
                parts[pid] = {"id": pid, "title": ptitle, "sections": [], "divisions": {}}
                order.append(pid)
            p = parts[pid]
            if did is None:
                p["sections"].append({"id": secid, "title": sectitle, "doc_id": _leg_id(act, secid)})
                continue
            if did not in p["divisions"]:
                p["divisions"][did] = {"id": did, "title": dtitle, "sections": [], "subdivisions": {}}
            d = p["divisions"][did]
            if sid is None:
                d["sections"].append({"id": secid, "title": sectitle, "doc_id": _leg_id(act, secid)})
                continue
            if sid not in d["subdivisions"]:
                d["subdivisions"][sid] = {"id": sid, "title": stitle, "sections": []}
            d["subdivisions"][sid]["sections"].append({"id": secid, "title": sectitle, "doc_id": _leg_id(act, secid)})
        out = []
        for pid in order:
            p = parts[pid]
            for d in p["divisions"].values():
                d["subdivisions"] = list(d["subdivisions"].values())
            p["divisions"] = list(p["divisions"].values())
            out.append(p)
        return out

    PAGE = 400
    offset = max(0, offset)

    if part:
        p = next((x for x in tree.get("parts", []) if x.get("id") == part), None)
        if p is None:
            return json.dumps({"error": f"Part '{part}' not found in {act}.",
                               "hint": _GET_INFO_HINT,
                               "available_parts": [x.get("id") for x in tree.get("parts", [])]}, indent=2)
        rows = _section_rows([p])
        page = rows[offset:offset + PAGE]
        truncated = offset + PAGE < len(rows)
        return json.dumps({
            "act": tree.get("act", act),
            "depth": f"sections (part: {part})",
            "sections_total": len(rows),
            "sections_returned": len(page),
            "offset": offset,
            "truncated": truncated,
            "next_offset": offset + len(page) if truncated else None,
            "truncation_note": ("Use offset=<n> to page further.") if truncated else None,
            "parts": _rebuild(page),
        }, indent=2)

    if depth == "parts":
        pruned = {
            "act": tree.get("act", act),
            "compilation_no": tree.get("compilation_no"),
            "compilation_date": _to_iso_date(tree.get("compilation_date")),
            "depth": "parts",
            "parts": [{"id": p.get("id"), "title": p.get("title")}
                      for p in tree.get("parts", [])
                      if p.get("id") or p.get("title")],  # filter empty nodes
        }
        return json.dumps(pruned, indent=2)
    elif depth == "divisions":
        pruned = {
            "act": tree.get("act", act),
            "compilation_no": tree.get("compilation_no"),
            "compilation_date": _to_iso_date(tree.get("compilation_date")),
            "depth": "divisions",
            "parts": [],
        }
        for p in tree.get("parts", []):
            if not p.get("id") and not p.get("title"):
                continue  # skip empty parts
            part_obj = {"id": p.get("id"), "title": p.get("title"), "divisions": []}
            for d in p.get("divisions", []):
                part_obj["divisions"].append({"id": d.get("id"), "title": d.get("title")})
            pruned["parts"].append(part_obj)
        return json.dumps(pruned, indent=2)

    # depth == "sections" — prune paths (not needed by LLM) and page at 400.
    # Full ITAA 1997 tree is ~1.1MB; offset walks the rest.
    rows = _section_rows(tree.get("parts", []))
    page = rows[offset:offset + PAGE]
    truncated = offset + PAGE < len(rows)
    return json.dumps({
        "act": tree.get("act", act),
        "compilation_no": tree.get("compilation_no"),
        "compilation_date": _to_iso_date(tree.get("compilation_date")),
        "depth": "sections",
        "sections_total": len(rows),
        "sections_returned": len(page),
        "offset": offset,
        "truncated": truncated,
        "next_offset": offset + len(page) if truncated else None,
        "truncation_note": ("Use offset=<n> to page further, or part=<part-id> to scope "
                            "to one part.") if truncated else None,
        "parts": _rebuild(page),
    }, indent=2)


async def list_treaty_articles(country: str) -> str:
    """List all articles for a given Double Tax Agreement country.

    Parameters:
    - country: Country slug (e.g. 'argentina', 'usa', 'china')

    Returns a list of articles with their number, title, and identifier.
    Use ``get_treaty_article`` to retrieve the full text of an article.
    """
    if (_denied := _mcp_denied("treaty")):
        return _denied
    tree_path = TREATIES_DIR / country / "tree.json"
    if not is_plain_id(country) or not tree_path.exists():
        available = sorted(
            d.name for d in TREATIES_DIR.iterdir()
            if d.is_dir() and (d / "tree.json").exists()
        )
        return json.dumps({
            "error": f"Treaty for '{country}' not found",
            "hint": _GET_INFO_HINT,
            "available_countries": available,
        }, indent=2)
    try:
        tree = json.loads(tree_path.read_text(encoding="utf-8"))
        return json.dumps({
            "treaty": tree.get("treaty", country),
            "country_slug": country,
            "schedule": tree.get("schedule"),
            "total_articles": tree.get("total", len(tree.get("articles", []))),
            "articles": [
                {"article": a["article"], "title": a["title"]}
                for a in tree.get("articles", [])
            ],
        }, indent=2)
    except Exception as e:
        logging.getLogger("lawkitty.mcp").exception("Failed to read treaty")
        return json.dumps({"error": "Failed to read treaty article", "hint": _GET_INFO_HINT}, indent=2)


# "... section 318 of the Income Tax Assessment Act 1936." (restored: lost in 81b6fb75b,
# which left get_definition raising NameError for cross-referencing definitions)
_DEF_XREF_RE = _re.compile(
    r"\b(?:sub)?section\s+([0-9][\w.\-]*?)(?:\((\d+)\))?\s+of\s+the\s+"
    r"([A-Z][A-Za-z0-9 ,()'\-]+?)(?=[.,;]|$)"
)


def _definition_cross_reference(act: str, text: str) -> dict | None:
    """Extract the target section of a cross-referencing definition.

    'associate has the meaning given by section 318 of the Income Tax
    Assessment Act 1936' -> leg:itaa-1936:318. Same-act references (e.g.
    'subsection 815-15(5)') resolve to the requested act.
    """
    if not text:
        return None
    from backend.doc_ids import leg_id
    m = _DEF_XREF_RE.search(text)
    if m:
        section, sub, title = m.group(1), m.group(2), m.group(3)
        slug = _act_slug_from_title(title) or act
        return {
            "act": slug,
            "section": section,
            "subsection": sub,
            "id": leg_id(slug, section, sub),
            "same_act": slug == act,
        }
    m2 = _re.match(
        r"^[^.]+?\b(?:has\s+the\s+meaning\s+given\s+by|is\s+defined\s+in)\s+"
        r"(?:sub)?section\s+([0-9][\w.\-]*)(?:\((\d+)\))?",
        text.strip(), _re.IGNORECASE,
    )
    if m2:
        section, sub = m2.group(1), m2.group(2)
        return {
            "act": act,
            "section": section,
            "subsection": sub,
            "id": leg_id(act, section, sub),
            "same_act": True,
        }
    return None


def _trim_definition_text(act: str, section: str, term: str, text: str) -> str:
    """Cut a definition snippet at the next defined-term boundary.

    get_definition_text's boundary logic assumes the next *alphabetically*
    adjacent term; where the section body lists a term that is not adjacent
    (e.g. 'associate' -> 'associated enterprises article') the snippet bleeds
    into later definitions. This scans every term defined in the section and
    truncates at the earliest subsequent definition start.
    """
    if not text or not section:
        return text
    try:
        from backend.services.data_loader import (
            load_definitions, _find_definition_start, _normalize_term_key,
        )
        defs = load_definitions(act)
        terms = sorted({k for k, v in defs.items() if v.get("section") == section})
    except Exception:
        return text
    key = _normalize_term_key(term)
    low = text.lower()
    after = len(key)
    idx = low.find(key)
    if idx != -1:
        after = idx + len(key)
    cut = len(text)
    for t in terms:
        if t == key:
            continue
        p = _find_definition_start(t, low, after)
        if p is None or not (after < p < cut):
            continue
        before = low[max(0, p - 60):p].rstrip()
        if before and before[-1] not in '.:;>"\n':
            continue
        cut = p
    return text[:cut].rstrip() if cut < len(text) else text


def _prepare_definition_result(result: dict) -> None:
    """Fix boundary bleed and attach the cross-reference target (in place)."""
    act = result.get("act", "")
    section = result.get("section", "")
    text = result.get("text") or ""
    if text:
        result["text"] = _trim_definition_text(act, section, result.get("term", ""), text)
        result["text_length"] = len(result["text"])
        result["truncated"] = bool(result["text"]) and not _re.search(
            r'[.)"\'!?;]\s*$', result["text"])
    for alt in result.get("also_defined_in", []) or []:
        if isinstance(alt, dict) and alt.get("text"):
            alt["text"] = _trim_definition_text(
                alt.get("act", ""), alt.get("section", ""),
                result.get("term", ""), alt["text"])
    if result.get("is_cross_reference"):
        xref = _definition_cross_reference(act, result.get("text") or "")
        if xref:
            result["cross_reference"] = xref


def _definition_suggestions(act: str, term: str) -> list[str]:
    """Nearest defined-term suggestions when a lookup misses (spec §5)."""
    import difflib
    try:
        from backend.services.data_loader import load_definitions, _normalize_term_key
        defs = load_definitions(act)
    except Exception:
        return []
    keys = list(defs.keys())
    close = difflib.get_close_matches(_normalize_term_key(term), keys, n=5, cutoff=0.7)
    out = []
    for k in close:
        sec = (defs.get(k) or {}).get("section", "")
        out.append(f"{k} (defined in {act} s {sec})" if sec else f"{k} (defined in {act})")
    return out


@mcp.tool(structured_output=False)
@_with_footer
async def get_definition(act: str, term: str) -> str:
    """Look up the definition of a term, searched across all acts.

    The requested act is preferred; matches in other acts (e.g. a term defined
    in ITAA 1936 s 318 rather than the requested act) are returned under
    ``also_defined_in``. When the definition is a cross-reference to another
    section, ``cross_reference`` carries that section's document ID.
    """
    if (_denied := _mcp_denied("legislation")):
        return _denied
    result = get_definition_across_acts(term, preferred_act=act)
    if result:
        _prepare_definition_result(result)
        return json.dumps(result, indent=2)
    return json.dumps({
        "error": f"Definition for '{term}' not found in any act",
        "suggestions": _definition_suggestions(act, term),
        "hint": _GET_INFO_HINT,
    })


RRF_K = 60


def _hybrid_sections(query: str, act: str | None, limit: int) -> list[dict]:
    """Blend BM25 (FTS5) and semantic (vector) section ranking via RRF.

    Keyword search alone ranks by literal phrase frequency and buries the
    conceptually-central provisions (e.g. "capital gains" -> roll-over and
    friendly-society sections instead of s 100-35 / s 102-5). The section
    embeddings already in data/embeddings.db carry the semantic signal, so
    fusing the two ranked lists fixes ranking with no new index to build.
    """
    n = max(limit, 20)
    fts = [r for r in fts_search(query, act, limit=n).get("results", [])
           if r.get("act") not in _INTERNAL_COLLECTIONS]
    try:
        from backend.services import searchd_client
        # searchd's HNSW walk first; the in-process matrix scan only when it is
        # down (None). An empty list is a real answer, not a reason to fall back.
        vec = None
        if searchd_client.available():
            vec = searchd_client.ann_candidates(
                query, limit=n, acts={act} if act else None, types={"section"},
            )
        if vec is None:
            from backend.services import vector_search_service as _vss
            vec = _vss.search(
                query, limit=n,
                acts={act} if act else None,
                source_types={"section"},
            )
    except Exception:
        vec = []

    vec = [v for v in (vec or []) if v.get("act") not in _INTERNAL_COLLECTIONS]
    if not vec:
        return _pin_concepts(query, act, fts, limit)

    scores: dict[tuple[str, str], float] = {}
    merged: dict[tuple[str, str], dict] = {}

    for rank, r in enumerate(fts):
        key = (str(r.get("act", "")), str(r.get("section", "")))
        scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
        merged.setdefault(key, r)

    for rank, v in enumerate(vec):
        key = (str(v.get("act", "")), str(v.get("section", "")))
        scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
        if key not in merged:
            merged[key] = {
                "act": v.get("act"),
                "section": v.get("section"),
                "title": v.get("title") or "",
                "part": None,
                "division": None,
                "snippet": v.get("snippet") or "",
                "rank": None,
            }

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return _pin_concepts(query, act, [merged[k] for k, _ in ranked], limit)


# ponytail: hand-curated pins for central provisions that BM25 buries because they live
# inside a huge section (s 6 ITAA 1936 is 45k chars, ranked 34/36 for "central management
# and control"). Ceiling: one entry per concept. Upgrade path: index subsections separately.
_CONCEPT_PINS = [
    # (trigger, unless: entity-specific residency has its own provision, e.g. 295-95, 102Q, 94T)
    (_re.compile(r"\bresiden(?:cy|ce)\b|\bcentral management and control\b", _re.I),
     _re.compile(r"super|fund|trust|partnership", _re.I),
     [("itaa-1936", "6", "Interpretation (definition of resident: company central "
                         "management and control test, s 6(1))")]),
]


def _pin_concepts(query: str, act: str | None, rows: list[dict], limit: int) -> list[dict]:
    for pat, unless, pins in _CONCEPT_PINS:
        if not pat.search(query) or unless.search(query):
            continue
        for p_act, p_sec, p_title in reversed(pins):
            if act and act != p_act:
                continue
            rows = [{"act": p_act, "section": p_sec, "title": p_title, "part": None,
                     "division": None, "snippet": "", "rank": None}] + [
                r for r in rows if (r.get("act"), str(r.get("section"))) != (p_act, p_sec)]
    return rows[:limit]


async def search_all(
    query: str,
    types: set[str] | None = None,
    act: str | None = None,
    status: str = "all",
    limit: int = 20,
) -> str:
    """Unified search across sections, cases, rulings, treaties, and RGs.

    Parameters:
    - query: Free-text search terms
    - types: Optional set of {"legislation","case","ruling","treaty","rg","afsa",
             "private_ruling"}. Default: legislation, case, ruling.
    - act: Optional — restrict to a specific act (e.g. 'itaa-1997')
    - status: "all" or "current" (excludes withdrawn/archived rulings)
    - limit: Max results per content type (default 20, max 50)

    Returns grouped results by type with snippets and metadata.
    """
    limit = min(50, max(1, limit))
    query = query.strip()
    if not query:
        return json.dumps({"error": "Query required", "results": {}, "hint": _GET_INFO_HINT})

    want = set(types) if types else {"legislation", "case", "ruling"}

    # Status filter applies to public rulings. search_rulings only knows how
    # to SQL-filter "current", so normalise once and post-filter the result
    # list — this also catches the exact-match row it pins to rank 1, which
    # would otherwise re-introduce a withdrawn (or non-withdrawn) ruling.
    status_norm = (status or "all").strip().lower()
    if status_norm not in ("all", "current", "withdrawn"):
        status_norm = "all"

    results = {}

    # Sections
    if "legislation" in want:
        try:
            results["sections"] = _hybrid_sections(query, act, limit)
        except Exception:
            results["sections"] = []

    # Rulings
    if "ruling" in want:
        try:
            results["rulings"] = search_rulings(
                query, limit=limit, status=status_norm)
        except Exception:
            results["rulings"] = []
        if status_norm == "withdrawn":
            results["rulings"] = [
                r for r in results["rulings"] if r.get("status") == "withdrawn"
            ]
        elif status_norm == "current":
            results["rulings"] = [
                r for r in results["rulings"] if r.get("status") != "withdrawn"
            ]

    # Private rulings (PBRs)
    if "private_ruling" in want:
        try:
            from backend.services.search_service import search_private_rulings_fts
            results["private_rulings"] = search_private_rulings_fts(query, limit=limit)
        except Exception:
            results["private_rulings"] = []

    # Cases — search via summaries
    if "case" in want:
        try:
            from backend.services.search_service import search_cases_fts
            case_results = search_cases_fts(query, limit * 2)
            case_results = [{
                "citation": r["citation"],
                "case_name": r["case_name"],
                "court": r["court"],
                "has_summary": True,
            } for r in case_results]
            results["cases"] = case_results[:limit]
        except Exception:
            results["cases"] = []

    # Treaties
    if "treaty" in want:
        try:
            results["treaties"] = search_treaties(query, limit=limit).get("results", [])
        except Exception:
            results["treaties"] = []

    # Regulatory guides
    if "rg" in want:
        try:
            results["regulatory_guides"] = search_rgs(query, limit=limit)
        except Exception:
            results["regulatory_guides"] = []

    # AFSA practice guidance (bankruptcy)
    if "afsa" in want:
        try:
            from backend.services.search_service import search_afsa_guides
            results["afsa_guides"] = search_afsa_guides(query, limit=limit)
        except Exception:
            results["afsa_guides"] = []

    for group, rows in results.items():
        for r in rows:
            _tag_content_type(r, _GROUP_SOURCE_TYPE.get(group))
    # Runtime content-type gating: the same gate as every other tool (fails closed).
    results = {g: _mcp_filter(rows) for g, rows in results.items()}
    return json.dumps({
        "query": query,
        "filter": sorted(want),
        "act": act,
        "results": _add_ids(results),
    }, indent=2)


# search_all result group -> the source_type its rows carry (content_type tagging, §7.2)
_GROUP_SOURCE_TYPE = {"sections": "section", "rulings": "ruling", "private_rulings": "private_ruling",
                      "cases": "case", "treaties": "treaty", "regulatory_guides": "regulatory_guide",
                      "afsa_guides": "afsa_guide"}


async def find_similar_rulings(query: str, limit: int = 10, outcome: str = "",
                               source: str = "all") -> str:
    """Semantic search over 57,608 ATO private rulings plus 12k+ public rulings.

    Embeds the query and returns the most similar rulings by meaning (not
    keyword match). For private rulings the response includes the QA pairs —
    the actual question the taxpayer asked and how the ATO answered (the
    'outcome' label: yes = favourable, no = unfavourable, mixed).

    Parameters:
    - query: a fact pattern, e.g. "contractor vs employee software developer
      home office" or "CGT main residence exemption deceased estate"
    - limit: max results (default 10, max 20)
    - outcome: filter private rulings by outcome label — yes | no | mixed
      (empty = no filter). 'no' answers = likely adverse ATO positions on the
      fact pattern (useful for objections); 'yes' = favourable positions.
    - source: private | public | all (default all)

    Returns each match's citation, title, date, score, snippet, ato_url and
    download_url links, and for private rulings the QA pairs and outcome label.
    """
    limit = min(20, max(1, limit))
    query = query.strip()
    if not query:
        return json.dumps({"total": 0, "results": [], "note": "Query required"})
    source = (source or "all").strip().lower()
    if source not in ("private", "public", "all"):
        source = "all"
    # Content-type gating: narrow `all` to the half the caller may see, refuse a hidden half.
    _ok_priv = not _mcp_denied("ato-private-ruling")
    _ok_pub = not _mcp_denied("ruling")
    if source == "all" and _ok_priv != _ok_pub:
        source = "private" if _ok_priv else "public"
    if (_denied := _mcp_denied("ato-private-ruling" if source == "private" else "ruling")):
        return _denied
    want_outcome = (outcome or "").strip().lower()
    if want_outcome not in ("", "yes", "no", "mixed"):
        want_outcome = ""

    from backend.services import vector_search_service as _vss
    from backend.services import searchd_client
    from backend.services import private_ruling_outcomes as _outcomes
    from backend.services.search_service import expand_semantic_query

    try:
        # Widen the query with tax-terminology synonyms before embedding so a
        # terse fact pattern isn't scored against the literal wording alone
        # (e.g. "crypto staking rewards ordinary income" must reach the ruling
        # that answers it, which phrases the concept as "received from holding
        # tokens as ordinary income").
        # searchd first, sliced to rulings at retrieval time so a ruling-only
        # query gets a full 50 candidates; the matrix scan only if it is down.
        sq = expand_semantic_query(query)
        top = None
        if searchd_client.available():
            top = searchd_client.ann_candidates(
                sq, limit=50, types={"ruling", "private_ruling"},
            )
            if top is not None:
                # an exact-reference hit carries no cosine; this tool ranks by it
                top = [h for h in top if h.get("score") is not None]
        if not top:   # [] as well as None: every searchd hit was an exact-ref hit with no cosine
            top = _vss.search(sq, limit=50)
    except Exception as exc:  # pragma: no cover - network/API failure path
        logging.getLogger("lawkitty.mcp").exception("vector search failed")
        return json.dumps({"error": "vector search failed"})

    # Keep rulings only; dedupe by (act, section) keeping best score per ruling
    best: dict[tuple[str, str], dict] = {}
    for r in top:
        st = r.get("source_type")
        if st not in ("ruling", "private_ruling"):
            continue
        if source == "private" and st != "private_ruling":
            continue
        if source == "public" and st != "ruling":
            continue
        key = (r["act"], r["section"])
        if key not in best or r["score"] > best[key]["score"]:
            best[key] = r

    ranked = sorted(best.values(), key=lambda r: -r["score"])[:limit]
    results = []
    for r in ranked:
        item = {
            "source": r.get("source_type"),  # private_ruling | ruling
            "citation": r.get("section"),
            "title": r.get("title"),
            "score": round(r["score"], 4),
            "snippet": r.get("snippet", ""),
        }
        if r.get("source_type") == "private_ruling":
            authnum = r.get("section") or ""
            item["ato_url"] = (f"https://www.ato.gov.au/law/view/print"
                               f"?DocID=EV/{authnum}&PiT=99991231235958")
            item["download_url"] = _abs(f"/api/private-ruling/{authnum}/download")
            rec = _outcomes.get(r.get("section") or "")
            if rec:
                item["date"] = rec.get("date_of_advice") or ""
                item["outcome"] = rec.get("outcome") or "unknown"
                item["qa"] = rec.get("qa") or []
                if not item["title"]:
                    item["title"] = rec.get("name") or ""
            else:
                item["outcome"] = "unknown"
            if want_outcome and item.get("outcome") != want_outcome:
                continue
        else:
            item["ato_url"] = r.get("ato_url", "") or ""
            item["download_url"] = _abs(f"/api/ruling/{r.get('section')}/download")
        results.append(item)

    return json.dumps({
        "total": len(results),
        "results": results,
        "note": "Semantic similarity over ATO private rulings (57,608) and public rulings. "
                "outcome reflects how the ATO answered the taxpayer's own question — "
                "a search aid, not a legal characterisation. Each result carries ato_url "
                "and download_url links. Verify against the full ruling before reliance "
                "(use fetch(ref=\"pbr:<authnum>\") for the edited text, or follow ato_url).",
    }, indent=2)


async def get_rg_sections(rg_number: int) -> str:
    """Retrieve the Corps Act sections cited by an ASIC Regulatory Guide.

    Parameters:
    - rg_number: The RG number (e.g. 1, 104, 140)

    Returns a list of sections referenced in the RG with their titles.
    """
    if (_denied := _mcp_denied("regulatory-guide")):
        return _denied
    import json as _json
    index = _load_rg_section_index()
    key = f"RG_{rg_number}"
    sections = index.get(key, [])
    if not sections:
        return _json.dumps({"rg_number": rg_number, "count": 0, "sections": []})
    # Enrich with titles
    try:
        tree = load_tree("corporations-act-2001")
        sec_map: dict[str, str] = {}
        for part in tree.get("parts", []):
            for sec in part.get("sections", []):
                sec_map[sec["id"]] = sec.get("title", "")
            for div in part.get("divisions", []):
                for sec in div.get("sections", []):
                    sec_map[sec["id"]] = sec.get("title", "")
                for sub in div.get("subdivisions", []):
                    for sec in sub.get("sections", []):
                        sec_map[sec["id"]] = sec.get("title", "")
        enriched = [{**s, "title": sec_map.get(s["section"], "")} for s in sections]
        sections = enriched
    except Exception:
        pass
    return _json.dumps({"rg_number": rg_number, "count": len(sections), "sections": sections}, indent=2)


async def get_afsa_sections(slug: str) -> str:
    """Retrieve the Bankruptcy Act sections cited by an AFSA practice doc."""
    if (_denied := _mcp_denied("afsa-guide")):
        return _denied
    import json as _json
    import sqlite3 as _sqlite3
    from backend.config import DATA_DIR
    sections: list[dict] = []
    graph_path = DATA_DIR / "graph.db"
    if graph_path.exists():
        try:
            conn = _sqlite3.connect(f"file:{graph_path}?mode=ro", uri=True)
            rows = conn.execute(
                """SELECT DISTINCT n.key, n.label FROM graph_edges e
                   JOIN nodes src ON src.id = e.source_id
                   JOIN nodes n ON n.id = e.target_id
                   WHERE src.key LIKE ? AND e.edge_type = 'cites' AND n.node_type = 'section'
                   ORDER BY n.label""",
                (f"afsa_guide:{slug}:%",),
            ).fetchall()
            conn.close()
            for key, label in rows:
                parts = key.split(":")
                sections.append({
                    "act": parts[1] if len(parts) > 1 else "",
                    "section": parts[-1] if len(parts) >= 3 else "",
                    "title": label,
                })
        except Exception:
            pass
    return _json.dumps({"slug": slug, "count": len(sections), "sections": sections}, indent=2)


@mcp.tool(structured_output=False)
@_with_footer
async def get_info() -> str:
    """Return server version, usage conventions, tool descriptions, and coverage counts.

    Call this FIRST before using any other tool, and call it AGAIN whenever a
    tool fails, returns null, or returns an unexpected empty result — the
    failure is usually a query-format or citation-format problem documented here.
    """
    content_types = _content_types()

    summaries_dir = str(DATA_DIR / ".." / "scripts" / "cleaned" / "summaries")
    summaries_count = len([f for f in os.listdir(summaries_dir)
                          if f.endswith(".json")]) if os.path.isdir(summaries_dir) else 0

    # Query issues table for known issues
    known_issues = []
    try:
        rows = _sql_dict(
            ["ticket", "tool", "known_note"],
            "SELECT ticket, tool, known_note FROM issues WHERE status = 'known' ORDER BY id",
        )
        for row in rows:
            known_issues.append({
                "ticket": row.get("ticket", ""),
                "tool": row.get("tool"),
                "note": row.get("known_note"),
            })
    except Exception:
        pass

    extra = {"private_collections": [_pkb.info_block()]} if _pkb_on() else {}
    return json.dumps({
        "name": content_types["instance"],
        "version": VERSION,
        **content_types,
        **extra,
        "usage": {
            "act_ids": [_a["id"] for _a in _public_act_entries()
                        if not _mcp_denied(_mcp_act_key(_a["id"]))],
            "document_ids": {
                "section": "leg:<act>:<section>  e.g. leg:itaa-1997:8-1",
                "subsection": "leg:<act>:<section>(<n>)  e.g. leg:itaa-1997:855-10(1)",
                "case": "case:<year>-<court>-<n>  e.g. case:2015-hca-48",
                "public ruling": "ruling:<series>-<year>-<n>  e.g. ruling:tr-2024-1",
                "private ruling": "pbr:<authnum>  e.g. pbr:1051234567890",
                "regulatory guide": "rg:<n>  e.g. rg:1",
                "treaty article": "dta:<country>:<article>  e.g. dta:usa:7",
                "nz section": "leg:nz-it-2007:CB-1  (CB1, cb-1 and 's CB 1 NZ Income Tax Act' also resolve)",
                "nz ruling": "BR Pub 24/01 or ruling:brpub-2024-1",
                **({} if _mcp_denied("commentary") else {
                    "commentary paragraph": "leg:master-tax-guide:4-200  (¶ number or section slug)"}),
            },
            "section_format": {
                "itaa-1997": "hyphenated: 8-1, 995-1",
                "itaa-1936": "unhyphenated: 23AH, 177D",
                "gst-1999": "hyphenated: 195-1",
            },
            "citation_format": {
                "rulings": "TR 2024/1 | PCG 2017/13 (types: TR TD PCG PS LA LCG AID IT CR GSTR MT PR SGR TA); 2-digit years auto-expanded; LCR=LCG",
                "cases": "[2024] HCA 1 bracketed medium-neutral (HCA FCAFC FCA); party-name aliases accepted",
            },
            "routing": {
                "find documents by words": "search",
                "find private rulings (fact patterns)": "search with types=['private_ruling']",
                "read one document": "fetch (accepts ID or citation)",
                "act or treaty structure": "outline",
                "citation links for a document": "related",
                "defined term": "get_definition",
                "browse public rulings": "list_rulings",
                "server info / conventions": "get_info",
                "content types and counts": "list_content_types",
                "bug or data gap": "report_issue",
            },
            "on_failure": "Fail/null/empty -> re-call get_info, reformat per the formats above, retry via search or fetch. Never guess a section number or invent a citation.",
            "rules": [
                "Answer only from tool results; if a tool returns nothing, say so. Never fill a gap from recall.",
                "A private ruling applies only to the applicant and is not precedent.",
                "report_issue does not change a citation's status.",
            ],
            "coverage": {
                "act_compilations": {
                    _a["id"]: {
                        "compilation_no": _a["compilation_no"],
                        "compilation_date": _a["compilation_date"],
                    }
                    for _a in _public_act_entries() if not _mcp_denied(_mcp_act_key(_a["id"]))
                },
                "summary_files_on_disk": summaries_count,
                "known_issues": known_issues,
                "note": "act_compilations gives each act's own compilation number and ISO date. "
                        "Per-type document counts are in the top-level content_types block; "
                        "summary_files is a separate filesystem count.",
            },
        },
    }, indent=2)


def _content_types() -> dict:
    from backend import data_sources
    from backend.config import INSTANCE, SOURCES
    types = data_sources.content_types(SOURCES)
    avail = _mcp_avail()
    types = [t for t in types if t.get("key") in avail]
    return {"instance": INSTANCE, "content_types": types}


@mcp.tool(structured_output=False)
@_with_footer
async def list_content_types() -> str:
    """List every content type on this instance: key, label, class, display,
    status (ok | not_configured | missing), document count and jurisdiction.

    The key is the content_type value on search results.
    """
    return json.dumps(_content_types(), indent=2)


# One citation-type code for ATO Interpretative Decisions: the search index
# (rulings_meta) and get_info() use 'AID'; the rulings loader's tree still uses
# the legacy 'ATOID' stem. Normalise output to AID and accept either on input.
_RULING_TYPE_CODE_ALIASES = {"ATOID": "AID"}


def _canon_ruling_type_code(t) -> str:
    u = str(t or "").upper().strip()
    return _RULING_TYPE_CODE_ALIASES.get(u, u)

@mcp.tool(structured_output=False)
@_with_footer
async def list_rulings(
    type: str | None = None,
    year: int | None = None,
    limit: int = 100,
    offset: int = 0,
    counts_only: bool = False,
) -> str:
    """List all ATO rulings grouped by year and type.

    Returns the full ruling tree with ATO.gov.au and AustLII links.
    """
    if (_denied := _mcp_denied("ruling")):
        return _denied
    rulings = load_rulings()

    if type:
        filter_type = _canon_ruling_type_code(type)
        rulings = [
            r for r in rulings
            if _canon_ruling_type_code(r.get("type", "")) == filter_type
        ]
    if year:
        rulings = [r for r in rulings if r.get("year") == year]

    if counts_only:
        years: dict = {}
        no_year: dict = {}
        for r in rulings:
            y = r.get("year", 0)
            t = _canon_ruling_type_code(r.get("type", "Ruling"))
            if y == 0:
                no_year[t] = no_year.get(t, 0) + 1
            else:
                if y not in years:
                    years[y] = {}
                years[y][t] = years[y].get(t, 0) + 1
        # Sort years ascending (numeric) and type keys within each year.
        years = {y: dict(sorted(v.items())) for y, v in sorted(years.items())}
        payload = {
            "mode": "counts_only",
            "total_rulings": len(rulings),
            "by_year": years,
        }
        if no_year:
            payload["no_year"] = dict(sorted(no_year.items()))
            payload["no_year_total"] = sum(no_year.values())
            payload["note"] = (
                f"Rulings without a year field ({sum(no_year.values())} total) "
                f"grouped under 'no_year'."
            )
        return json.dumps(payload, indent=2)

    if limit > 0:
        rulings = rulings[offset:offset + limit]
    elif offset > 0:
        rulings = rulings[offset:]

    years_dict: dict = {}
    no_year_items = []
    for r in rulings:
        y = r.get("year", 0)
        t = _canon_ruling_type_code(r.get("type", "Ruling"))
        if y == 0:
            no_year_items.append(r)
            continue
        if y not in years_dict:
            years_dict[y] = {}
        if t not in years_dict[y]:
            years_dict[y][t] = []
        years_dict[y][t].append({
            "citation": r["citation"],
            "citation_display": r.get("citation_display", ""),
            "title": r.get("full_title", r.get("title", "")),
            "withdrawn": r.get("withdrawn", False),
            "ato_url": r.get("ato_url", ""),
            "austlii_url": r.get("austlii_url", ""),
            "download_url": _abs(f"/api/ruling/{r['citation']}/download"),
        })

    # Sort years ascending and type keys within each year (LK-34).
    years_dict = {
        y: dict(sorted(v.items())) for y, v in sorted(years_dict.items())
    }
    payload = {
        "ato_rulings_total": len(rulings),
        "filter_type": type,
        "filter_year": year,
        "by_year": years_dict,
    }
    if no_year_items:
        payload["no_year"] = [
            {"citation": r["citation"], "title": r.get("full_title", r.get("title", "")),
             "withdrawn": r.get("withdrawn", False), "ato_url": r.get("ato_url", ""),
             "austlii_url": r.get("austlii_url", ""),
             "download_url": _abs(f"/api/ruling/{r['citation']}/download")}
            for r in no_year_items
        ]
        payload["no_year_total"] = len(no_year_items)
        payload["note"] = (
            f"Rulings without a year field ({len(no_year_items)} total) listed under 'no_year'."
        )

    return json.dumps(payload, indent=2)


@mcp.tool(structured_output=False)
@_with_footer
async def report_issue(
    category: Literal["bad_data", "missing_content", "stale_compilation", "wrong_result", "tool_error", "suggestion", "bug"],
    tool: str | None = None,
    params: dict | str | None = None,
    expected: str | None = None,
    actual: str | None = None,
    note: str | None = None,
) -> str:
    """Report a bug or issue with a tool's output.

    Detects duplicates by (param_hash, category). If a matching open or
    known issue already exists, increments the hit counter and returns the
    existing ticket. Otherwise creates a new issue (CDN-NNNN) in the
    issues table.

    Args:
        category: Type of issue being reported.
        tool: Name of the tool that produced the wrong output.
        params: Parameters that were passed to the tool.
        expected: What the correct output should have been.
        actual: What the tool actually returned.
        note: Free-form notes about the issue.

    Returns:
        JSON with ticket, status, and duplicate_of keys.
    """
    # ── guard: reject if nothing meaningful was provided ─────────────────────
    has_content = any(
        str(v).strip()
        for v in (tool, note, expected, actual)
        if v is not None
    )
    if params not in (None, "", {}, []):
        has_content = True
    if not has_content:
        return json.dumps({
            "error": "report_issue requires at least one of tool, params, expected, actual, note",
            "ticket": None,
            "status": "rejected",
        })
    # ── compute param_hash (includes note to avoid hash collision) ────────
    raw_hash = ""
    if tool:
        if isinstance(params, dict):
            canonical_params = json.dumps(params, sort_keys=True, ensure_ascii=True, default=str)
        elif isinstance(params, str):
            try:
                p = json.loads(params)
                canonical_params = json.dumps(p, sort_keys=True, ensure_ascii=True, default=str)
            except (json.JSONDecodeError, TypeError):
                canonical_params = params
        else:
            canonical_params = "null"
        raw_hash = tool + canonical_params + (note or "")
    elif note:
        raw_hash = note
    else:
        raw_hash = str(category)
    param_hash = hashlib.sha256(raw_hash.encode("utf-8")).hexdigest()[:16]

    params_val = json.dumps(params, default=str) if isinstance(params, (dict, list)) else params

    # ── write-only insert into the lawkitty Postgres (parameterized) ────────
    from backend import db
    new_id = None
    ticket = None
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO issues (category, tool, params, param_hash, expected, actual, note, server_ver, status, hits) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open', 1) RETURNING id",
                (category, tool, params_val, param_hash, expected, actual, note, VERSION),
            )
            row = cur.fetchone()
            if row:
                new_id = row["id"]
                ticket = f"CDN-{new_id:04d}"
                cur.execute("UPDATE issues SET ticket = %s WHERE id = %s", (ticket, new_id))
    except Exception:
        logging.getLogger(__name__).exception("report_issue insert failed")

    if new_id is None:
        return json.dumps({"error": "Failed to create issue ticket", "ticket": None, "status": "rejected"})

    return json.dumps({
        "ticket": ticket,
        "status": "open",
        "duplicate_of": None,
    })


@mcp.tool(structured_output=False)
async def graph_neighbourhood(key: str, depth: int = 1) -> str:
    """Return the graph context block for a node (graph spec §6.2).

    Gives an LLM the compact neighbourhood of any graph node without
    fetching full document text: per-edge-type counts + top exemplars.

    Args:
        key:    canonical graph key, e.g. "section:itaa-1997:118-110",
                "public_ruling:TR 2025/1", "case:[2015] HCA 48",
                "private_ruling:EV/1011261243735", "commentary:MTG:ch12".
                Accepts the resolved_by graph_key from resolve_alias.
                Also accepts leg:<act>:<section>, <act>:<section>, ruling:/pbr:/case:
                document ids and plain citations ("s 8-1 ITAA 1997", "TR 2024/1").
        depth:  1 = node neighbourhood (~80 tokens); 2 = + aggregated
                neighbourhood-of-neighbourhood (<=400 tokens). Default 1.

    Returns:
        JSON {key, label, depth, tokens, text} where `text` is the
        token-lean block (header + lines like "INTERPRETED_BY: 4 rulings
        (TR 2025/1 | TD 2024/2)"). Unknown keys return an error object.
    """
    if depth not in (1, 2):
        return json.dumps({"error": "depth must be 1 or 2", "hint": _GET_INFO_HINT}, indent=2)
    from backend.services.graph_serialize import serialize as _serialize

    # Several id conventions live in this server (graph keys, leg:/case:/ruling:/pbr: doc ids,
    # plain citations). Normalise to graph keys, then gate on the normalised keys.
    from backend.services.mcp_ids import graph_key_candidates
    candidates = graph_key_candidates(key)
    _avail = _mcp_avail()
    if not all(node_allowed(_avail, c) for c in candidates):
        return json.dumps({  # denied reads exactly as unknown
            "error": f"unknown graph key: {key}",
            "hint": _GET_INFO_HINT,
            "format_hint": "Try related first, or /api/graph/data on the REST API.",
        }, indent=2)
    _deny = frozenset(nt for nt, k in NODE_TYPE_KEY.items() if k not in _avail)
    out = None
    for cand in candidates:
        out = _serialize(cand, depth=depth, deny=_deny)
        if out is not None:
            key = cand
            break
    if out is None:
        return json.dumps({
            "error": f"unknown graph key: {key}",
            "hint": _GET_INFO_HINT,
            "format_hint": "Try related first, or /api/graph/data on the REST API.",
        }, indent=2)
    return json.dumps(out, indent=2)


@mcp.tool(structured_output=False)
async def graph_path(from_key: str, to_key: str, max_hops: int = 10) -> str:
    """Find the shortest path between two graph nodes (graph spec §6.3).

    Answers "how does this connect to that?" — e.g. the chain of
    rulings/cases/commentary linking a private ruling to a High Court case.

    Args:
        from_key: canonical graph key, e.g. "private_ruling:EV/1011261243735".
        to_key:   canonical graph key, e.g. "case:[1986] HCA 45".
        max_hops: hop cap (1-20, default 10).

    Returns:
        JSON {from, to, hops, path, edges} — path is the ordered node list
        (key + label per hop), edges give the typed connection per hop.
        Unreachable pairs return path: null with a reason.
    """
    import sqlite3 as _sqlite3
    from pathlib import Path as _Path

    from backend.services.graph_path import (
        FRONTIER_CAP,
        FrontierExceeded,
        find_path as _find_path,
    )

    from backend.services.mcp_ids import graph_key_candidates

    max_hops = max(1, min(int(max_hops), 20))
    _avail = _mcp_avail()
    _cands = {k: graph_key_candidates(k) for k in (from_key, to_key)}
    for _k, _cs in _cands.items():
        if not all(node_allowed(_avail, c) for c in _cs):
            return json.dumps({  # denied reads exactly as unknown
            "error": f"unknown graph key: {_k}",
            "hint": _GET_INFO_HINT,
            "format_hint": "Try related first, or /api/graph/data on the REST API.",
        }, indent=2)
    graph_db = _Path(__file__).resolve().parents[1] / "data" / "graph.db"
    conn = _sqlite3.connect(str(graph_db))
    try:
        def _resolve(k: str) -> int | None:
            for c in _cands[k]:
                row = conn.execute("SELECT id FROM nodes WHERE key=?", (c,)).fetchone()
                if row is None:
                    row = conn.execute(
                        "SELECT id FROM nodes WHERE lower(key)=?", (c.lower(),)).fetchone()
                if row:
                    return row[0]
            return None

        f_id = _resolve(from_key)
        t_id = _resolve(to_key)
        if f_id is None or t_id is None:
            missing = from_key if f_id is None else to_key
            return json.dumps({
                "error": f"unknown graph key: {missing}",
                "hint": _GET_INFO_HINT,
                "format_hint": "Try related first, or /api/graph/data on the REST API.",
            }, indent=2)

        try:
            path, hops = _find_path(conn, f_id, t_id, max_hops=max_hops)
        except FrontierExceeded as exc:
            return json.dumps({
                "from": from_key, "to": to_key, "path": None, "hops": None,
                "reason": f"frontier exceeded {exc} nodes at a level (cap {FRONTIER_CAP})",
            }, indent=2)

        if path is None:
            return json.dumps({
                "from": from_key, "to": to_key, "path": None, "hops": None,
                "reason": f"no path within {max_hops} hops",
            }, indent=2)

        ids = [nid for nid, _ in path]
        meta: dict[int, tuple[str, str]] = {}
        if ids:
            ph = ",".join("?" * len(ids))
            meta = {r[0]: (r[1], r[2]) for r in conn.execute(
                f"SELECT id, key, label FROM nodes WHERE id IN ({ph})", ids).fetchall()}
        # ponytail: a path through a hidden type is withheld, not re-routed around it.
        if not all(node_allowed(_avail, meta.get(n, ("", ""))[0] or "") for n in ids):
            return json.dumps({"from": from_key, "to": to_key, "path": None, "hops": None,
                               "reason": f"no path within {max_hops} hops"}, indent=2)

        return json.dumps({
            "from": from_key,
            "to": to_key,
            "hops": hops,
            "path": [
                {"key": meta.get(nid, (None, str(nid)))[0],
                 "label": meta.get(nid, (None, str(nid)))[1]}
                for nid in ids
            ],
            "edges": [
                {"type": et, "from": i, "to": i + 1}
                for i, (_, et) in enumerate(path[1:], start=0)
                if et is not None
            ],
        }, indent=2)
    finally:
        conn.close()




# ────────────────────────── quoting tool (standalone) ──────────────────────────

# =====================================================================
# lawkitty MCP v2 — 9-tool surface (spec v2 §4)
# search / search_private_rulings / fetch / outline / related +
# get_definition / list_rulings / get_info / report_issue
# =====================================================================

@mcp.tool(structured_output=False)
@_with_footer
async def search(
    query: str,
    types: list[str] | None = None,
    act: str | None = None,
    status: str = "current",
    limit: int = 10,
    cursor: str | None = None,
) -> str:
    """Hybrid keyword search over legislation, cases and public rulings.

    types is a subset of legislation|case|ruling|rg|afsa|treaty|private_ruling
    (default: legislation, case, ruling). private_ruling uses semantic
    fact-pattern matching; the rest are keyword. Find IDs here, then call
    fetch to read a document.
    commentary|insolvency|nz_mtg|nz_ruling are not search-indexed: alone they
    return not_indexed (or not_available), mixed in they are listed under
    skipped_types. Unknown types are an invalid_param error.
    """
    limit = min(50, max(1, limit))
    _VALID = {"legislation", "case", "ruling", "rg", "afsa", "treaty", "private_ruling"}
    if isinstance(types, str):
        types = [types]
    asked = [str(t).strip().lower().replace("-", "_") for t in (types or [])]
    # _pkb_on() costs a grants lookup: ask it only when the call names the KB (or on error)
    kb_on = bool(_pkb is not None and (_pkb.SEARCH_TYPE in asked or (act or "").strip().lower() == _pkb.KEY)
                 and _pkb_on())
    _accepted = _VALID | ({_pkb.SEARCH_TYPE} if kb_on else set())
    bad = [t for t in asked if t not in _accepted and t not in _SEARCH_UNINDEXED]
    if bad:
        if _pkb is not None and not kb_on and _pkb_on():
            _accepted = _accepted | {_pkb.SEARCH_TYPE}
        raise _tool_error("invalid_param", f"unknown search type(s) {bad}", sorted(
            _accepted | {t for t, k in _SEARCH_UNINDEXED.items() if not _mcp_denied(k)}))
    kb_act = bool(kb_on and act and act.strip().lower() == _pkb.KEY)
    kb_hits = None
    if kb_on and (kb_act or _pkb.SEARCH_TYPE in asked):
        kb_hits = _mcp_filter([_tag_content_type(r, _pkb.SEARCH_TYPE) for r in _pkb.search(query, limit)])
        if kb_act or not any(t in _VALID for t in asked):
            # KB only: the shared index never holds KB rows, so search_all is not called
            out = {"query": query, "filter": [_pkb.SEARCH_TYPE], "results": {_pkb.SEARCH_TYPE: kb_hits}}
            sk = {t: _search_unavailable(_SEARCH_UNINDEXED[t])[1] for t in asked if t in _SEARCH_UNINDEXED}
            if sk:
                out["skipped_types"] = sk
            return json.dumps(out, indent=2)
        asked = [t for t in asked if t != _pkb.SEARCH_TYPE]
    # Requested types (or an act) with no search index: say why instead of silently
    # searching the defaults. Gate first so a standard caller only ever sees "not available".
    skipped = {t: _search_unavailable(_SEARCH_UNINDEXED[t]) for t in asked if t in _SEARCH_UNINDEXED}
    if act:
        act = act.strip().lower()
        akey = act if act in ("commentary", "insolvency", "nz-mtg") else act_key(act)
        if akey in _SEARCH_UNINDEXED.values():
            code, msg = _search_unavailable(akey)
            raise _tool_error(code, msg)
    want = {t for t in asked if t in _VALID}
    if not want and skipped:
        codes = {c for c, _ in skipped.values()}
        raise _tool_error("not_available" if codes == {"not_available"} else "not_indexed",
                          "; ".join(m for _, m in skipped.values()))
    if not want:
        want = {"legislation", "case", "ruling"}
    if "private_ruling" in want:
        kw = want - {"private_ruling"}
        result = json.loads(await search_all(query=query, types=kw, act=act, status=status, limit=limit)) if kw else {"results": {}}
        pr = json.loads(await find_similar_rulings(query=query, limit=min(20, limit), source="private"))
        result["results"]["private_rulings"] = _mcp_filter([_tag_content_type(r, "private_ruling")
                                                            for r in pr.get("results", [])])
        result["filter"] = sorted(want)
    else:
        result = json.loads(await search_all(query=query, types=want, act=act, status=status, limit=limit))
    if kb_hits is not None:
        result.setdefault("results", {})[_pkb.SEARCH_TYPE] = kb_hits
    if skipped:
        result["skipped_types"] = {t: m for t, (_, m) in skipped.items()}
    return json.dumps(result, indent=2)


# search types with no FTS/vector index -> content key (verified 2026-11-24: search_index.db
# has no master-tax-guide / keays / nz-master-tax-guide sections and no NZ rulings).
_SEARCH_UNINDEXED = {"commentary": "commentary", "insolvency": "insolvency",
                     "nz_mtg": "nz-mtg", "nz_ruling": "nz-ruling"}


def _search_unavailable(key: str) -> tuple[str, str]:
    if _mcp_denied(key) and not _hidden(key):
        return "not_available", f"{key} is not available to your account"
    # a hidden type answers a denied caller exactly as it answers one who may read it
    return "not_indexed", (f"{key} is not in the search index; browse with outline / fetch"
                           " (e.g. fetch('BR Pub 24/01') for NZ rulings)" if key == "nz-ruling" else
                           f"{key} is not in the search index; browse with outline / fetch")


_RESOLVED_TYPE_KEY = {"case": "case", "private_ruling": "ato-private-ruling", "rg": "regulatory-guide",
                      "afsa": "afsa-guide", "treaty": "treaty"}
if _pkb is not None:
    _RESOLVED_TYPE_KEY["kb"] = _pkb.KEY  # second check behind the fetch hook


def _deny_ref(ref: str) -> None:
    """Gate a leg: ID on its act BEFORE resolving, so a denied caller gets not_available
    whether or not the section exists (otherwise not_found vs not_available enumerates
    restricted sections)."""
    from backend.doc_ids import parse_id
    from backend.services.data_loader import resolve_act_id
    d = parse_id(ref)
    if d is not None and d.type == "legislation":
        if d.id.split(":")[1] in _INTERNAL_COLLECTIONS:  # internal display spec, not a source
            raise _tool_error("not_found", f"no document for {ref!r}")
        key = act_key(resolve_act_id(d.id.split(":")[1]))
        if _mcp_denied(key):
            if _hidden(key):
                raise _tool_error("not_found", f"no document for {ref!r}")
            raise _tool_error("not_available", f"{key} is not available to your account")


def _deny_resolved(result: dict, ref: str | None = None) -> None:
    """Raise when a resolve_ref() document is a type the MCP caller may not read."""
    t = result.get("type")
    if result.get("collection") and _mcp_denied(act_key(result["collection"])):
        # a case carrying its judgment from the case-law collection is gated by that collection too
        raise _tool_error("not_found", f"no document for {(ref or result.get('id'))!r}")
    if t == "legislation":
        key = act_key(result.get("act") or "")
    elif t == "ruling":
        key = "nz-ruling" if str(result.get("jurisdiction") or "").upper() == "NZ" else "ruling"
    else:
        key = _RESOLVED_TYPE_KEY.get(t)
    if key and _mcp_denied(key):
        if _hidden(key):
            raise _tool_error("not_found", f"no document for {(ref or result.get('id'))!r}")
        raise _tool_error("not_available", f"{key} is not available to your account")


# search_private_rulings was merged into search (use search types=["private_ruling"]).


# Most text one fetch returns (a range or 'all' is cut at a chunk boundary below this).
FETCH_MAX_CHARS = 60_000


@mcp.tool(structured_output=False)
@_with_footer
async def fetch(ref: str | None = None, chunk: int | str = 1, with_notice: bool = False,
                as_at: str | None = None,
                refs: list[str] | str | None = None) -> str:
    """Return one document by document ID or human citation.

    Large documents are split into ~8000-character chunks on paragraph /
    sentence boundaries. ``chunk`` takes a single number (``2``), a range
    (``'2-4'``), a list (``'1,3'``), or ``'all'`` to return the whole document
    in one call — so once you have a document ID from search you can read it
    without paging. The response carries ``total_chunks``, the chunks returned,
    and ``next_chunk`` for anything after them. Use ``'all'`` deliberately: on a
    very long document it returns a lot of text in one response.

    Source attribution is deliberately NOT attached to every fetch (it would be
    paid on every call). Call ``notice(ref)`` once when you need the exact
    licence wording for a source — for example before quoting it — or pass
    ``with_notice=True`` for the compact one-line version on this call only.

    ref examples: 'leg:itaa-1997:8-1', 's 8-1 ITAA 1997', 'TR 2024/1',
    '[2024] HCA 1', 'rg:1', 'rg:103:5.15' (single paragraph), 'dta:usa:7',
    'afsa:<slug>' (AFSA practice guidance), 'afsa:<slug>:<para>',
    'pbr:1051234567890'.
    """
    from backend.resolve_ref import _REQUESTED_AS_AT, resolve_ref

    if refs:
        # Batch fetch (CDN-0297). Each item is resolved independently so one bad ref cannot
        # lose the rest of the call.
        wanted = json.loads(refs) if isinstance(refs, str) else list(refs)
        if isinstance(wanted, str) or not isinstance(wanted, (list, tuple)) \
                or not all(isinstance(x, str) for x in wanted):
            raise _tool_error("bad_refs", "refs must be a JSON array of reference strings.",
                              ['Pass refs=["leg:itaa-1997:8-1","leg:itaa-1997:8-2"].'])
        capped = len(wanted) > 50
        items = []
        for one in wanted[:50]:
            try:
                items.append(json.loads(await fetch(ref=str(one), chunk=chunk,
                                                    with_notice=with_notice, as_at=as_at)))
            except Exception as exc:  # noqa: BLE001
                items.append({"ref": str(one), "error": f"{error_label(exc)}: {exc}"})
        out = {"count": len(items), "items": items}
        if capped:
            # Never let the caller believe a capped batch was the whole request.
            out["items_omitted"] = len(wanted) - len(items)
            out["note"] = (f"Batch capped at 50 refs; {len(wanted) - len(items)} omitted. "
                           f"Split the request to cover them.")
        return json.dumps(out, indent=2)

    if not ref:
        raise _tool_error("empty_ref", "Empty reference.",
                          ["Pass ref='leg:<act>:<section>' or refs=['leg:...','leg:...']."])

    if _pkb is not None and _pkb.owns(ref) and _pkb_on():
        try:
            result = _pkb.fetch(ref)
        except LookupError:
            raise _tool_error("not_found", f"no document for {ref!r}")
    else:
        # The token must be reverted, or as_at leaks into every later call served by this task.
        _deny_ref(ref)
        _as_at_token = _REQUESTED_AS_AT.set(as_at)
        try:
            result = resolve_ref(ref)
        finally:
            _REQUESTED_AS_AT.reset(_as_at_token)
        if "error" in result:
            e = result["error"]
            raise _tool_error(e["code"], e["message"], _fetch_suggestions(ref, e))
        _deny_resolved(result, ref)
    # Real chunking (spec §4.1): split the document body, return one slice.
    body_key = next(
        (k for k in ("text", "body", "content")
         if isinstance(result.get(k), str) and result.get(k)),
        "text",
    )
    body_text = result.get(body_key) or ""
    # Strip rendering scaffolding and scraped markup BEFORE chunking so the
    # returned text is clean prose, not raw HTML / markdown (LK-26).
    if result.get("type") == "kb":
        pass  # our own rendered markdown: no scraped markup to strip
    elif result.get("type") == "legislation" or _re.search(
            r'<a\s+id="s|\*\*\([0-9A-Za-z]+\)\*\*', body_text):
        body_text = clean_legislation_body(body_text)
    else:
        body_text = strip_scraped_markup(body_text)
    chunks = _split_into_chunks(body_text)
    total = len(chunks)
    if not body_text.strip():
        # The citation exists in the collection but no summary text is held for it (e.g. an
        # unsummarised judgment). Say so instead of returning a silent empty body.
        result["notice"] = ("No summary text is held for this document — the citation is in the "
                            "collection but has not been summarised. Metadata and catchwords, "
                            "where present, are still returned.")
    try:
        wanted, clamp_note = _parse_chunk_spec(chunk, total)
    except ValueError as exc:
        raise _tool_error(
            "chunk_out_of_range",
            f"{exc} for {result.get('id') or ref} ({total} chunks, valid 1..{total})",
            [f"Use chunk 1..{total}, a range like '2-4', a list like '1,3', or 'all'."],
        )
    # One response stays readable: a range or 'all' over a long judgment stops at the size cap and
    # says where to carry on, rather than sending text the client would cut off.
    kept, size = [], 0
    for i in wanted:
        if kept and size + len(chunks[i - 1]) > FETCH_MAX_CHARS:
            break
        kept.append(i)
        size += len(chunks[i - 1])
    if len(kept) < len(wanted):
        cap_note = (f"returned chunks {kept[0]}-{kept[-1]} of {total}: one response is capped at about "
                    f"{FETCH_MAX_CHARS:,} characters; continue with chunk={kept[-1] + 1}")
        clamp_note = (clamp_note + " " + cap_note) if clamp_note else cap_note
        wanted = kept
    result[body_key] = "\n\n".join(chunks[i - 1] for i in wanted)
    result["total_chunks"] = total
    if len(wanted) == 1 and isinstance(chunk, int):
        result["chunk"] = wanted[0]          # unchanged shape for a single chunk
    else:
        result["chunk_range"] = str(chunk)
        result["chunk_count"] = len(wanted)
    result["next_chunk"] = wanted[-1] + 1 if wanted[-1] < total else None
    if clamp_note:
        # append — never clobber the "no summary held" note set above
        result["notice"] = (result["notice"] + " " + clamp_note) if result.get("notice") else clamp_note
    # LK-26: ASIC RGs carry their publication date in long form
    # ('21 April 2026'); present ISO 8601 across the MCP surface.
    if result.get("type") == "rg" and result.get("date"):
        result["date_display"] = result["date"]
        result["date"] = _to_iso_date(result["date"])
    # Source attribution is NOT attached unless explicitly asked for: a per-fetch line is paid on
    # every call an agent makes. The `notice` tool returns it once, on demand.
    if with_notice:
        notice = _source_notice(result)
        if notice:
            result["source_notice"] = notice
    return json.dumps(result, indent=2)


@mcp.tool(structured_output=False)
@_with_footer
async def notice(ref: str) -> str:
    """Return the source, attribution and reuse terms for a document reference.

    Call this once per source when you need the exact wording — for example
    before quoting the material in a document you are drafting, or to check
    whether a source may be reproduced at all. Document fetches do not carry
    the notice on every call, so this is the way to get it without paying for
    it repeatedly.

    Returns the compact source line, the full attribution wording, any
    unofficial-copy / no-endorsement notice, and links to the licence and the
    publisher's terms. A null ``notice`` means no reuse notice applies to that
    source (it is not cleared for display, e.g. treaty texts of unverified
    provenance).
    """
    from backend.resolve_ref import resolve_ref
    if _pkb is not None and _pkb.owns(ref) and _pkb_on():
        try:
            return json.dumps(_pkb.notice(ref), indent=2)
        except LookupError:
            raise _tool_error("not_found", f"no document for {ref!r}")
    _deny_ref(ref)
    result = resolve_ref(ref)
    if "error" in result:
        e = result["error"]
        raise _tool_error(e["code"], e["message"], _fetch_suggestions(ref, e))
    _deny_resolved(result, ref)
    from backend.services.data_loader import document_licence, licence_notice
    t = (result.get("type") or "").lower()
    if t in ("legislation", "section") and result.get("act"):
        lic = licence_notice(result["act"])
    elif t in ("ruling", "private_ruling", "private-ruling"):
        lic = document_licence("private-rulings" if t.startswith("private") else "rulings",
                               source_url=result.get("ato_url") or None)
    elif t == "rg":
        lic = document_licence("regulatory-guides", source_url=result.get("page_url") or None)
    elif t == "case":
        lic = document_licence("tax-cases", result.get("citation") or result.get("id") or "")
    else:
        lic = None
    if not lic:
        return json.dumps({"ref": ref, "notice": None,
                           "why": "No reuse notice applies to this source in this product."}, indent=2)
    out = {"ref": ref, "source_notice": _source_notice(result)}
    for k in ("jurisdiction", "notice_badge", "notice_line", "attribution", "disclaimer",
              "unofficial_notice", "endorsement_notice", "source_url", "terms_url", "version_date"):
        if lic.get(k) not in (None, "", "None"):
            out[k] = lic[k]
    out["licence"] = lic.get("licence")
    return json.dumps(out, indent=2)


@mcp.tool(structured_output=False)
@_with_footer
async def outline(
    id: str | None = None,
    depth: str = "sections",
    cursor: str | None = None,
) -> str:
    """Structure of a collection or document.

    id=None lists collections (acts + treaty countries). id=<act-slug> returns
    the act tree (depth: parts|divisions|sections). id=<country> returns treaty
    articles.
    """
    if id is None:
        acts = json.loads(await list_acts())
        countries = [] if _mcp_denied("treaty") else sorted(
            d.name for d in TREATIES_DIR.iterdir()
            if d.is_dir() and (d / "tree.json").exists()
        )
        collections = {"acts": acts.get("acts", []), "treaties": countries}
        if _pkb_on():
            collections["private"] = [_pkb.collection_row()]
        return json.dumps({"collections": collections}, indent=2)
    if _pkb is not None and id == _pkb.KEY and _pkb_on():
        return json.dumps(_pkb.outline(depth), indent=2)
    if not _re.fullmatch(r"[a-z0-9-]+", id):
        return json.dumps({"error": f"Invalid id '{id}'.", "hint": _GET_INFO_HINT}, indent=2)
    if (DATA_DIR / id / "tree.json").exists() and not _mcp_hidden_denied(id):
        return await get_act_tree(act=id, depth=depth)
    if (TREATIES_DIR / id / "tree.json").exists():
        return await list_treaty_articles(country=id)
    return json.dumps({"error": f"Unknown id '{id}'.", "hint": _GET_INFO_HINT}, indent=2)


@mcp.tool(structured_output=False)
@_with_footer
async def related(
    id: str,
    direction: str = "both",
    types: list[str] | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> str:
    """Deterministic citation links for a document.

    One consistent, pageable schema for every document type:

      {id, type, direction, limit,
       cites:      [{id, type, citation, title}],   # forward citations
       cited_by:   [{id, type, citation, title}],   # reverse citations
       cites_total, cited_by_total, next_cursor, truncated}

    id is a document ID: 'case:<year>-<court>-<n>', 'rg:<n>', 'afsa:<slug>',
    or 'leg:<act>:<section>' (also accepted: '<act>:<section>' and graph keys such
    as 'section:<act>:<section>'). limit caps each of cites/cited_by independently;
    pass next_cursor to page. Every entry carries a fetchable document id
    (leg:/case:/ruling:/pbr:). cited_by for legislation comes from the graph;
    the reverse index for cases is a deferred build and returns [].
    """
    from backend.doc_ids import parse_id
    from backend.services.mcp_ids import related_doc_id

    # Any id form naming a hidden act the caller cannot use (leg:<act>:s, <act>:s, section:<act>:s)
    # answers exactly as the same id with an act this instance lacks.
    for _tok in str(id or "").split(":"):
        if _tok and _tok != _UNKNOWN_ACT and _mcp_hidden_denied(_tok):
            alias = ":".join(_UNKNOWN_ACT if t == _tok else t for t in str(id).split(":"))
            return (await related(alias, direction, types, limit, cursor)).replace(_UNKNOWN_ACT, _tok)

    if _pkb is not None and _pkb.owns(id) and _pkb_on():
        return json.dumps(_pkb.related(id, _mcp_avail()), indent=2)
    id = related_doc_id(id)  # accept itaa-1997:8-1, section:..., graph-key forms
    docid = parse_id(id)
    if docid is None:
        return json.dumps({
            "error": f"Invalid id '{id}'.",
            "suggestions": ["Use a document id such as case:2024-hca-1, "
                            "leg:itaa-1997:8-1, rg:1 or afsa:<slug>."],
            "hint": _GET_INFO_HINT,
        }, indent=2)

    _src_key = {"case": "case", "rg": "regulatory-guide", "afsa": "afsa-guide"}.get(docid.type)
    if docid.type == "legislation":
        _src_key = _mcp_act_key(id.split(":", 2)[1] if id.count(":") >= 2 else "")
    if _src_key and (_denied := _mcp_denied(_src_key)):
        return _denied

    limit = min(50, max(1, limit))
    cites: list[dict] = []
    cited_by: list[dict] = []

    if docid.type == "case":
        year, court, n = id.split(":", 1)[1].split("-", 2)
        citation = f"[{year}] {court.upper()} {n}"
        refs = get_case_references(citation)
        cites += [i for i in (_leg_ref_item(r) for r in refs.get("legislation_refs", [])) if i]
        cites += [i for i in (_case_ref_item(r) for r in refs.get("case_citations", [])) if i]
        cited_by += [i for i in (_case_ref_item(r) for r in refs.get("cited_by", [])) if i]

    elif docid.type == "legislation":
        act, section = id.split(":", 1)[1].split(":", 1)
        for c in _graph_cases_for_section(act, section, limit=1000):
            cites_rev = {"type": "case", "citation": c.get("citation", ""),
                         "title": c.get("title") or c.get("short_name", ""),
                         "id": _case_item_id(c.get("citation", ""))}
            cited_by.append(cites_rev)
        for r in _graph_rulings_for_section(act, section, limit=1000):
            cited_by.append(_ruling_link_item(r.get("citation", ""), r.get("title", "")))
        for p in _graph_private_rulings_for_section(act, section, limit=1000):
            auth = p.get("auth_number", "")
            cited_by.append({
                "type": "private_ruling",
                "citation": f"private ruling {auth}" if auth else "",
                "title": p.get("label", ""),
                "id": f"pbr:{auth}" if auth else "",
            })

    elif docid.type == "rg":
        n = int(id.split(":", 2)[1])
        data = json.loads(await get_rg_sections(rg_number=n))
        for s in data.get("sections", []):
            sec = str(s.get("section", "")).strip()
            if not sec:
                continue
            cites.append({
                "type": "legislation",
                "id": f"leg:corporations-act-2001:{sec}",
                "citation": f"Corporations Act 2001 s {sec}",
                "title": s.get("title", ""),
                "act": "corporations-act-2001",
                "section": sec,
            })

    elif docid.type == "afsa":
        slug = id.split(":", 2)[1]
        data = json.loads(await get_afsa_sections(slug=slug))
        for s in data.get("sections", []):
            act = str(s.get("act", "")).strip()
            sec = str(s.get("section", "")).strip()
            cites.append({
                "type": "legislation",
                "id": f"leg:{act}:{sec}" if (act and sec) else "",
                "citation": f"{act} s {sec}".strip(),
                "title": s.get("title", ""),
                "act": act,
                "section": sec,
            })

    else:
        return json.dumps({
            "error": f"related not supported for type '{docid.type}'.",
            "suggestions": ["Supported: case, legislation, rg, afsa."],
            "hint": _GET_INFO_HINT,
        }, indent=2)

    # content-type gating: drop linked items the caller may not read
    _avail = _mcp_avail()

    def _ok(i: dict) -> bool:
        if i.get("type") == "legislation" and i.get("act"):
            return act_key(i["act"]) in _avail
        return node_allowed(_avail, i.get("type") or "")
    cites = [i for i in cites if _ok(i)]
    cited_by = [i for i in cited_by if _ok(i)]

    # type filter
    if types:
        want = set(types)
        cites = [i for i in cites if i.get("type") in want]
        cited_by = [i for i in cited_by if i.get("type") in want]

    # direction filter
    dirn = (direction or "both").strip().lower()
    if dirn in ("forward", "out", "cites", "cite"):
        cited_by = []
    elif dirn in ("backward", "in", "cited_by", "cited-by", "back"):
        cites = []

    # page each list independently (spec §4.1)
    offs = _decode_cursor(cursor)
    c_off = max(0, int(offs.get("c", 0) or 0))
    b_off = max(0, int(offs.get("b", 0) or 0))
    cites_total = len(cites)
    cited_by_total = len(cited_by)
    page_cites = cites[c_off:c_off + limit]
    page_cited_by = cited_by[b_off:b_off + limit]
    truncated = (c_off + len(page_cites) < cites_total
                 or b_off + len(page_cited_by) < cited_by_total)
    next_cursor = None
    if truncated:
        next_cursor = _encode_cursor({
            "c": c_off + len(page_cites),
            "b": b_off + len(page_cited_by),
        })

    return json.dumps({
        "id": id,
        "type": docid.type,
        "direction": direction,
        "limit": limit,
        "cites": page_cites,
        "cited_by": page_cited_by,
        "cites_total": cites_total,
        "cited_by_total": cited_by_total,
        "next_cursor": next_cursor,
        "truncated": truncated,
    }, indent=2)


@mcp.tool(structured_output=False)
async def report_bad_recall(query: str, expected: str | None = None, note: str | None = None) -> str:
    """Flag a failed recall: a search or fetch that missed what it should have found.

    query is what was searched; expected is the document that should have come back
    (e.g. 's 8-1 ITAA 1997', 'TR 2024/1'); note is optional context. Logged for the
    LawKitty team as a failed recall. Use report_issue for wrong data or tool bugs.
    """
    ev = usage_log.current.get() or {}
    # Persist it: a recall failure that only echoes back is a lost signal. Same table as
    # report_issue so the LawKitty queue sees both kinds in one place.
    from backend import db
    ticket = None
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO issues (category, tool, params, param_hash, expected, actual, note,"
                " server_ver, status, hits)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open', 1) RETURNING id",
                ("wrong_result", "report_bad_recall",
                 json.dumps({"query": query}, default=str),
                 hashlib.sha256((query + (expected or "")).encode()).hexdigest()[:16],
                 expected, ev.get("request_id"), note, VERSION),
            )
            row = cur.fetchone()
            if row:
                ticket = f"CDN-{row['id']:04d}"
                cur.execute("UPDATE issues SET ticket = %s WHERE id = %s", (ticket, row["id"]))
    except Exception:
        logging.getLogger(__name__).exception("report_bad_recall insert failed")
        return json.dumps({"recorded": False, "error": "failed to record the failed recall",
                           "query": query})
    return json.dumps({"recorded": True, "ticket": ticket, "request_id": ev.get("request_id"),
                       "query": query, "expected": expected})


# Every tool above is registered; wrap each once so its calls fill the usage event and share
# one availability lookup.
_memoise_avail_per_call(mcp)
usage_log.wrap_tools(mcp)


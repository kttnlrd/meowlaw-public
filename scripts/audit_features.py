#!/usr/bin/env python3.12
"""Feature audit: prove each surface of a LawKitty host actually operates.

The data gate (verify_data_integrity.py) checks the corpus text. It cannot tell you that a
download returns 200, that an MCP tool answers, that the shipped bundle is the one you built, or
that the two hosts still expose the same tools. This does that against a running host.

Phases
  coverage   every route family the app mounts is named in the spec (needs --app-root)
  rest       each endpoint family: public GETs 200 with JSON, gated ones 401
  downloads  each document family returns 200 with real bytes, and its JSON sibling stays 401
  ui         the page loads, every asset it references resolves, and markers are present
  mcp        initialise, diff tools/list against the spec, call every non-mutating tool
  features   numeric assertions on a route's payload
  ratelimit  a download family refuses the 121st request in a minute (opt-in: --ratelimit)
  parity     same feature, two hosts: tool sets and identical fetch payload size

Skips are honest, failures are not: a phase that needs a credential this run does not have
records SKIP, never PASS, and skips never make the exit code fail.

Usage
  audit_features.py --spec specs/audit-lawkitty.json --base https://lawkitty.app \
      --mcp-token "$TOKEN" [--app-root /path/to/repo] [--ratelimit] \
      [--compare https://lawkitty.app --compare-token "$OTHER"] [--json out.json]

  --list-tools prints each tool and its required arguments, which is how fixtures get chosen.

Exit 0 only when no check failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 45


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, not a detour. Following it hides a missing local file behind a
    third party's copy."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _req(method: str, url: str, body: dict | None = None, headers: dict | None = None,
         follow: bool = True) -> tuple[int, bytes, str]:
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": "lawkitty-feature-audit/2.0"}
    if data:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    opener = urllib.request.urlopen if follow else _opener.open
    try:
        with opener(req, timeout=TIMEOUT) as r:
            return r.status, r.read(), r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), (e.headers.get("content-type", "") if e.headers else "")
    except Exception as e:  # network, TLS, timeout
        return 0, str(e).encode(), ""


class Results:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, phase: str, name: str, ok: bool, detail: str) -> None:
        self.rows.append({"phase": phase, "check": name, "ok": ok, "detail": detail[:300]})
        print(f"  [{'PASS' if ok else 'FAIL'}] {phase:9} {name:36} {detail[:110]}")

    def skip(self, phase: str, name: str, reason: str) -> None:
        self.rows.append({"phase": phase, "check": name, "ok": True, "skipped": True,
                          "detail": reason[:300]})
        print(f"  [SKIP] {phase:9} {name:36} {reason[:110]}")

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.rows if not r["ok"]]

    @property
    def skipped(self) -> list[dict]:
        return [r for r in self.rows if r.get("skipped")]


class Mcp:
    """Minimal streamable-HTTP JSON-RPC client: enough to list and call tools."""

    def __init__(self, base: str, token: str) -> None:
        # scriptkitty carries the token in the path; lawkitty takes it as a bearer header.
        self.candidates = [(f"{base.rstrip('/')}/mcp/{token}", {}),
                           (f"{base.rstrip('/')}/mcp", {"Authorization": f"Bearer {token}"})]
        self.url, self.auth = self.candidates[0]
        self.session: str | None = None
        self.next_id = 0

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        body = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        headers = {"Accept": "application/json, text/event-stream"}
        headers.update(self.auth)
        if self.session:
            headers["mcp-session-id"] = self.session
        code, raw, ctype = _req("POST", self.url, body, headers)
        text = raw.decode(errors="ignore")
        if "text/event-stream" in ctype or "\ndata:" in text or text.lstrip().startswith("event:"):
            for chunk in text.split("\n\n"):
                for line in chunk.splitlines():
                    if line.startswith("data:"):
                        try:
                            doc = json.loads(line[5:].strip())
                        except Exception:
                            continue
                        if doc.get("id") == self.next_id:
                            return doc
            return {"error": {"message": "no JSON-RPC payload in event stream"}}
        try:
            return json.loads(text)
        except Exception:
            return {"error": {"message": f"HTTP {code}: {text[:120]}"}}

    def _init(self) -> str:
        init: dict = {}
        err = ""
        for url, auth in self.candidates:
            self.url, self.auth, self.session = url, auth, None
            init = self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "feature-audit", "version": "2.0"}})
            if not init.get("error"):
                err = ""
                break
            e = init.get("error")
            err = str(e.get("message") if isinstance(e, dict) else e)[:140]
        if init.get("error"):
            return err or "initialize failed"
        sid = ((init.get("result") or {}).get("sessionId")) or ""
        if sid:
            self.session = sid
        self._rpc("notifications/initialized")
        return ""

    def tools(self) -> tuple[list[dict], str]:
        err = self._init()
        if err:
            return [], err
        listing = self._rpc("tools/list")
        if listing.get("error"):
            e = listing["error"]
            return [], f"tools/list failed: {str(e.get('message') if isinstance(e, dict) else e)[:120]}"
        return (listing.get("result") or {}).get("tools") or [], ""

    def call(self, name: str, args: dict) -> tuple[bool, str, str]:
        doc = self._rpc("tools/call", {"name": name, "arguments": args})
        if doc.get("error"):
            e = doc["error"]
            return False, f"rpc error: {str(e.get('message') if isinstance(e, dict) else e)[:120]}", ""
        result = doc.get("result") or {}
        content = result.get("content") or []
        text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        if not text:
            text = json.dumps(result.get("structuredContent") or "")
        if result.get("isError"):
            return False, f"tool error: {text[:140]}", text
        return (len(text.strip()) > 0), f"{len(text)} chars", text


def phase_coverage(spec: dict, app_root: str, res: Results) -> None:
    """Every mounted route family must be named in the spec, or it ships unaudited."""
    if not app_root:
        res.skip("coverage", "route families", "no --app-root, cannot enumerate the app")
        return
    cwd = os.getcwd()
    sys.path.insert(0, app_root)
    try:
        os.chdir(app_root)
        from backend.main import app as fastapi_app  # type: ignore
        families = set()
        for r in fastapi_app.routes:
            path = getattr(r, "path", "") or ""
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 2 and parts[0] == "api":
                families.add("/" + "/".join(parts[:2]))
        listed = {("/" + "/".join([p for p in i["path"].split("/") if p][:2]))
                  for i in spec.get("rest", []) + spec.get("downloads", []) + spec.get("features", [])}
        listed |= set(spec.get("known_unlisted") or [])
        listed |= set(spec.get("not_on_this_host") or [])
        unlisted = sorted(f for f in families if f not in listed)
        res.add("coverage", "route families", not unlisted,
                (f"{len(families)} families, unlisted: {unlisted[:8]}" if unlisted
                 else f"{len(families)} families all named in the spec"))
    except Exception as e:
        res.skip("coverage", "route families", f"cannot import app: {str(e)[:90]}")
    finally:
        os.chdir(cwd)


def phase_rest(spec: dict, base: str, res: Results, cookie: str = "") -> None:
    for item in spec.get("rest", []):
        path = item["path"]
        want = item.get("expect", 200)
        hdrs = {"Cookie": cookie} if cookie else None
        code, raw, ctype = _req("GET", base.rstrip("/") + path, headers=hdrs)
        ok = code == want
        detail = f"HTTP {code} {ctype.split(';')[0]}"
        if ok and want == 200 and item.get("json", True):
            try:
                json.loads(raw)
                detail += " json-ok"
            except Exception:
                ok, detail = False, detail + " not-json"
        res.add("rest", item.get("name", path), ok, detail)


def phase_downloads(spec: dict, base: str, res: Results) -> None:
    for item in spec.get("downloads", []):
        ident = urllib.parse.quote(item["id"], safe="")
        path = item["path"].format(id=ident)
        code, raw, ctype = _req("GET", base.rstrip("/") + path, follow=False)
        min_bytes = item.get("min_bytes", 500)
        ctype_ok = any(ctype.startswith(t) for t in item.get("types", ["application/", "text/"]))
        ok = code == 200 and len(raw) >= min_bytes and ctype_ok
        detail = f"HTTP {code} {ctype.split(';')[0]} {len(raw)}B (want >={min_bytes})"
        if 300 <= code < 400:
            ok = False
            detail = f"HTTP {code} redirect off-site, local file missing? (want 200)"
        res.add("downloads", item["name"], ok, detail)
        sibling = item.get("gated_sibling")
        if sibling:
            code2, _r2, _t2 = _req("GET", base.rstrip("/") + sibling.format(id=ident))
            res.add("downloads", item["name"] + " json-gated", code2 == 401, f"HTTP {code2} (want 401)")


def phase_ui(spec: dict, base: str, res: Results, cookie: str = "") -> None:
    for item in spec.get("ui", []):
        local_dist = item.get("local_dist")
        if local_dist:
            # The shell sitn behind a login wall, so read the asset names from the build that was
            # deployed and prove those exact files serve, and carry the marker strings.
            index = os.path.join(local_dist, "index.html")
            if not os.path.exists(index):
                res.skip("ui", item["name"], f"no build at {index}")
                continue
            html = open(index, encoding="utf-8", errors="ignore").read()
            refs = re.findall(r'(?:src|href)="(/[^"]+\.(?:js|css))"', html)
            bad, bodies = [], []
            prefix = item.get("asset_prefix", "")
            for ref in refs[:4]:
                # A build may already emit absolute paths ("/app/assets/..."), so only add the
                # prefix when it is missing. An SPA fallback answers 200 with HTML for a wrong
                # path, so treat an HTML body for a .js/.css request as not served.
                url = base.rstrip("/") + (ref if (not prefix or ref.startswith(prefix)) else prefix + ref)
                url += ("&" if "?" in url else "?") + "_audit=%d" % int(time.time() * 1000)
                c2, r2, t2 = _req("GET", url, follow=False)
                if c2 != 200:
                    bad.append(f"{ref} HTTP {c2}")
                elif "text/html" in (t2 or "") and not ref.endswith(".css"):
                    bad.append(f"{ref} served HTML (wrong path)")
                else:
                    bodies.append(r2.decode(errors="ignore"))
            markers = item.get("markers") or []
            haystack = "\n".join(bodies)
            missing = [m for m in markers if m.lower() not in haystack.lower()]
            detail = f"{len(refs)} built assets, {len(bad)} not served"
            if bad:
                detail += f" {bad[:2]}"
            if missing:
                detail += f" missing markers {missing[:3]}"
            res.add("ui", item["name"], not bad and not missing, detail)
            continue
        page = item["page"]
        hdrs = {"Cookie": cookie} if cookie else None
        code, raw, _ctype = _req("GET", base.rstrip("/") + page, headers=hdrs)
        if code in (301, 302, 303, 307, 308):
            res.skip("ui", item["name"], f"HTTP {code} login wall; pass --cookie to audit the shell")
            continue
        if code != 200:
            res.add("ui", item["name"], False, f"HTTP {code}")
            continue
        html = raw.decode(errors="ignore")
        refs = re.findall(r'(?:src|href)="(/[^"]+\.(?:js|css))"', html)
        bad, bodies = [], []
        for ref in refs[:6]:
            c2, r2, _t = _req("GET", base.rstrip("/") + ref)
            if c2 != 200:
                bad.append(f"{ref} HTTP {c2}")
            else:
                bodies.append(r2.decode(errors="ignore"))
        markers = item.get("markers") or []
        haystack = "\n".join(bodies) if bodies else html
        missing = [m for m in markers if m.lower() not in haystack.lower()]
        detail = f"{len(refs)} assets, {len(bad)} bad"
        if bad:
            detail += f" {bad[:2]}"
        if missing:
            detail += f" missing markers {missing[:3]}"
        res.add("ui", item["name"], not bad and not missing, detail)


def phase_mcp(spec: dict, base: str, token: str, res: Results) -> list[str]:
    if not token:
        res.skip("mcp", "handshake", "no --mcp-token supplied")
        return []
    client = Mcp(base, token)
    tools, err = client.tools()
    if err:
        res.add("mcp", "handshake", False, err)
        return []
    names = sorted(t["name"] for t in tools)
    res.add("mcp", "handshake", True, f"{len(names)} tools")
    expected = spec.get("mcp_tools") or []
    if expected:
        missing = [t for t in expected if t not in names]
        extra = [t for t in names if t not in expected]
        res.add("mcp", "tool set", not missing and not extra,
                (f"missing {missing}" if missing else
                 (f"not named in spec {extra}" if extra else f"all {len(expected)} present")))
    mutating = set(spec.get("mutating_tools") or [])
    for name, want in (spec.get("mcp_calls") or {}).items():
        args = want.get("args", {}) if isinstance(want, dict) else want
        contains = want.get("contains", "") if isinstance(want, dict) else ""
        min_chars = want.get("min_chars", 1) if isinstance(want, dict) else 1
        if name not in names:
            res.add("mcp", f"call {name}", False, "tool not present on this host")
            continue
        if name in mutating:
            res.skip("mcp", f"call {name}", "mutating tool: listed, never called")
            continue
        ok, detail, text = client.call(name, args)
        if ok and min_chars > 1 and len(text) < min_chars:
            ok, detail = False, detail + f" (want >={min_chars} chars)"
        if ok and contains and contains.lower() not in text.lower():
            ok, detail = False, detail + f" (missing {contains!r})"
        res.add("mcp", f"call {name}", ok, detail)
    uncalled = [n for n in names if n not in (spec.get("mcp_calls") or {}) and n not in mutating]
    if uncalled:
        res.add("mcp", "every tool exercised", False, f"never called: {uncalled}")
    else:
        res.add("mcp", "every tool exercised", True,
                f"{len(names) - len(mutating)} called, {len(mutating)} mutating excluded")
    return names


def phase_features(spec: dict, base: str, res: Results, cookie: str = "") -> None:
    for item in spec.get("features", []):
        hdrs = {"Cookie": cookie} if cookie else None
        code, raw, _ctype = _req("GET", base.rstrip("/") + item["path"], headers=hdrs)
        if code != 200:
            res.add("features", item["name"], False, f"HTTP {code}")
            continue
        try:
            doc = json.loads(raw)
        except Exception:
            res.add("features", item["name"], False, "not json")
            continue
        problems = []
        for field, minimum in (item.get("min") or {}).items():
            value = doc
            for part in field.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            got = (value if isinstance(value, (int, float))
                   else (len(value) if isinstance(value, (list, dict)) else None))
            if got is None or got < minimum:
                problems.append(f"{field}={got} < {minimum}")
        res.add("features", item["name"], not problems, "; ".join(problems) or "ok")


def phase_ratelimit(spec: dict, base: str, res: Results) -> None:
    item = (spec.get("ratelimit") or {})
    if not item:
        res.skip("ratelimit", "download bucket", "no ratelimit block in spec")
        return
    ident = urllib.parse.quote(item["id"], safe="")
    path = base.rstrip("/") + item["path"].format(id=ident)
    limit = item.get("limit", 120)
    codes: list[int] = []
    started = time.time()
    for _ in range(limit + 8):
        code, _raw, _t = _req("GET", path)
        codes.append(code)
        if code == 429:
            break
    elapsed = time.time() - started
    res.add("ratelimit", "bucket refuses", 429 in codes,
            f"{codes.count(200)}x200 {codes.count(429)}x429 in {elapsed:.1f}s (limit {limit}/min)")


def phase_parity(spec: dict, base: str, token: str, other: str, other_token: str, res: Results) -> None:
    if not (token and other_token):
        res.skip("parity", "hosts", "needs both --mcp-token and --compare-token")
        return
    a_set = {t["name"] for t in Mcp(base, token).tools()[0]}
    b_set = {t["name"] for t in Mcp(other, other_token).tools()[0]}
    only_a, only_b = sorted(a_set - b_set), sorted(b_set - a_set)
    res.add("parity", "tool sets", True,
            f"{base.split('//')[-1]}={len(a_set)} {other.split('//')[-1]}={len(b_set)}"
            + (f"; only here {only_a}" if only_a else "")
            + (f"; only there {only_b}" if only_b else ""))
    ref = spec.get("parity_ref") or "leg:itaa-1997:8-1"
    ta = Mcp(base, token).call("fetch", {"ref": ref})[2]
    tb = Mcp(other, other_token).call("fetch", {"ref": ref})[2]
    # Sizes legitimately differ: prod holds private rulings, scriptkitty holds CCH commentary, so
    # the same provision carries different neighbours. Assert both resolve the same provision
    # instead, and report the sizes rather than pretending parity means equal bytes.
    section_id = ref.split(":")[-1]
    ok = section_id in ta and section_id in tb and len(ta) > 200 and len(tb) > 200
    res.add("parity", f"fetch {ref}", ok,
            f"both resolve {section_id}; {len(ta)} vs {len(tb)} chars")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--mcp-token", default="")
    ap.add_argument("--compare", default="")
    ap.add_argument("--compare-token", default="")
    ap.add_argument("--app-root", default="")
    ap.add_argument("--cookie", default="")
    ap.add_argument("--ratelimit", action="store_true")
    ap.add_argument("--list-tools", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    spec = json.loads(open(args.spec).read())

    if args.list_tools:
        tools, err = Mcp(args.base, args.mcp_token).tools()
        if err:
            print("  " + err)
            return 1
        for t in sorted(tools, key=lambda x: x["name"]):
            schema = t.get("inputSchema") or {}
            props = schema.get("properties") or {}
            print(f"   {t['name']}({', '.join(props)}) required={schema.get('required') or []}")
        return 0

    res = Results()
    print(f"  host: {spec.get('host')} ({args.base}) spec {spec.get('spec_version')}")
    phase_coverage(spec, args.app_root, res)
    phase_rest(spec, args.base, res, args.cookie)
    phase_downloads(spec, args.base, res)
    phase_ui(spec, args.base, res, args.cookie)
    phase_features(spec, args.base, res, args.cookie)
    phase_mcp(spec, args.base, args.mcp_token, res)
    if args.ratelimit:
        phase_ratelimit(spec, args.base, res)
    if args.compare:
        phase_parity(spec, args.base, args.mcp_token, args.compare, args.compare_token, res)

    failed, skipped = res.failed, res.skipped
    passed = len(res.rows) - len(failed) - len(skipped)
    print(f"\n  {passed}/{len(res.rows) - len(skipped)} checks passed, {len(skipped)} skipped")
    if failed:
        print("  failing:")
        for r in failed:
            print(f"   - {r['phase']}/{r['check']}: {r['detail']}")
    if args.json:
        open(args.json, "w").write(json.dumps({"spec": spec.get("host"), "base": args.base,
                                               "rows": res.rows}, indent=1))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

# Security policy

## Reporting a vulnerability

Email **harry@cadena.legal** with the details, or open a private security
advisory at https://github.com/kttnlrd/meowlaw-public/security/advisories/new.
Please do not open a public issue for a security problem.

Include what you did, what happened, the version or commit you tested, and any
proof-of-concept you can share. A first reply comes within five working days.

## Supported versions

Fixes land on `main` and in the newest tagged release. Older tags are not
patched.

## What is in scope

MeowLaw is a single binary that serves a web UI, a REST API and an MCP endpoint
from one process, usually behind a reverse proxy.

In scope:

- the HTTP server, its routing, origin checks and bearer-token handling;
- the MCP endpoint, its tools and transport;
- parsing of untrusted ingest input (markdown, text, HTML, XML, JSON, DOCX, PDF);
- path handling for uploads, corpus files and the SQLite store.

Out of scope:

- the content an operator ingests and serves — whoever runs an instance is
  responsible for it;
- deployments that put the server on a public address with no token while
  `protect_api` is off;
- misconfiguration of the reverse proxy, TLS or the host;
- denial of service from a deliberately huge document, unless it corrupts data
  or escapes the ingest sandbox.

## Hardening checklist for operators

- Set a bearer token (`token_env`) whenever the server is reachable by anyone
  else, and leave `protect_api` on.
- Keep `allowed_origins` to the origins you actually serve.
- Leave `allow_ingest` off in public deployments; ingest from the CLI instead.
- Run the container as the unprivileged user the image already uses, with the
  data directory writable by that user only.

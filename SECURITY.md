# Security policy

## Reporting a vulnerability

Report privately through GitHub's [private vulnerability reporting](https://github.com/kttnlrd/meowlaw-public/security/advisories/new).
Do not open a public issue for a security bug.

Please include the version or commit, what an attacker gains, and the smallest
reproduction you have. We aim to acknowledge within five working days and to
ship a fix or a mitigation for confirmed issues.

## Supported versions

Only the latest tagged release and `main` receive security fixes while the
project is pre-1.0.

## Scope

MeowLaw is a self-hosted engine with an HTTP server, an MCP endpoint and a
local SQLite store. In scope:

- Authentication and authorisation bypasses in the server or the MCP transport.
- SQL injection, path traversal, and template or command injection in any
  ingestion path. Ingested documents are untrusted input.
- Cross-site request forgery against the HTTP API, and cross-origin data
  exposure.
- Denial of service that survives the configured request and body limits.
- Secrets or credentials written to logs.

Out of scope: misconfiguration of a deployment you run (see the checklist
below), and vulnerabilities in third-party content committed under
`examples/`.

## Hardening checklist for operators

The defaults are chosen for a local, single-user instance. Before exposing an
instance to a network:

- Set a strong MCP token and a strong session secret. Never ship the example
  values from `meowlaw.example.toml`.
- Set `allowed_origins` to the exact hosts you serve. An empty or wildcard list
  turns off the origin check.
- Leave `allow_ingest` off on any instance strangers can reach. Ingestion is a
  privileged operation.
- Bind to `127.0.0.1` and put a reverse proxy in front when the instance must be
  public, and terminate TLS there.
- Keep the data directory and configuration file readable only by the service
  account.

## Disclosure

We will credit reporters in the release notes unless you ask us not to.

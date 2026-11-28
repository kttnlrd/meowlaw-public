# Contributing — Legislation Explorer

## Git Workflow

### Branching
- **`master`** is the only permanent branch. It is always deployable.
- Every feature, fix, or experiment gets its own branch off `master`.
- Branch naming: `feature/<short-description>`, `fix/<short-description>`, `chore/<short-description>`.
- Delete remote branches after merging. Delete local branches after confirming the merge.

### Commits
- Commit after **every meaningful change** — a new component, a working endpoint, a data pipeline run. Not "every save", but not "everything at end of session" either.
- Commit messages: present tense, concise. Example: `Add search autocomplete with debounce` not `Added search autocomplete with debounce (fixed bug)`
- Do not commit generated/build artifacts (`node_modules/`, `__pycache__/`, `.pyc`, `dist/`, `.env`). `.gitignore` handles these — if something sneaks in, amend or rebase it out.

### Push Frequency
- Push **every 2–3 commits**, or at the end of any coding session. "Push early, push often."
- A pushed branch is safe. An unpushed commit is one `git checkout --` away from disaster.
- Before ending a session: commit working state, push branch, confirm remote is up to date.

### Merging
- Merge feature branches into `master` with `--no-ff` (preserves branch history).
- After merging: push `master`, delete the remote feature branch.
- Squash merge is forbidden — individual commits tell the story of how something was built.

## Code Standards

### General
- Type hints on all Python functions. No `Any` unless unavoidable.
- React components: one component per file, named export, TypeScript.
- SVG icons over emoji in UI. Resilient flexbox layouts with `flexWrap: wrap`.
- No commented-out code. If something is dead, delete it. Git has the history.

### Backend (Python/FastAPI)
- Routes in `backend/routes/`, one file per domain (search, acts, rulings, etc.).
- Business logic in `backend/services/`, not in route handlers.
- New endpoints: add to `api.py` or create a new route file + register in `main.py`.
- Async where possible (DB queries, HTTP calls).

### Frontend (React/TypeScript)
- Components in `frontend/src/components/`.
- API calls in `frontend/src/api.ts` — centralised, typed.
- Styling: inline styles or CSS modules. No Tailwind, no styled-components.
- Vite dev server runs on port 3000. Prod build served by FastAPI on port 8765.

### Data Pipelines
- Scraper scripts in `pipeline/`. One script per data source.
- Never commit raw scraped HTML. Extract structured data first.
- Data files go in `data/`. Large files (>50 MB) should be gitignored and documented.
- All scrapers must handle rate limiting (sequential requests, ~0.4s delay minimum).
- Verify content by title tag or content check, not HTTP status code alone (Akamai returns 200 for error pages).

## Testing & Verification

### Before Merging
1. Frontend builds without errors: `cd frontend && npx vite build`
2. Backend starts without import errors: `cd backend && python -c "from main import app"`
3. Critical user flow works: search → click result → view content → navigate tree
4. If data changed: verify a few live URLs to confirm content is fresh

## Coding Tools

### Implementation — Kimi Code
- Primary coding tool: `kimi` (`~/.local/bin/kimi`).
- Use for writing new features, refactoring, data pipelines, and bug fixes.
- Kimi implements; Claude reviews.

### Code Review — Claude Code
- Before merging non-trivial branches, request a Claude Code review:
  `claude --dangerously-skip-permissions --review`
- Review should catch: type errors, missing edge cases, performance issues, dead code.
- Claude does NOT do implementation unless Kimi is unavailable or the task is Claude-specific.

### Debugging (Ralph Loop)
1. First attempt: fix and retry (use Kimi Code).
2. Second attempt: diagnose root cause, fix with more context (still Kimi).
3. Third attempt: escalate — delegate to Claude Code with full context, or rethink approach.

### Feature Design Process (for complex features)
For non-trivial features, use the competitive multi-model workflow:
1. Claude Code + Kimi Code both write implementation plans independently.
2. Kimi reviews Claude's plan, then implements.
3. Claude reviews/verifies the implementation.

## Environments
- **Dev:** `dev.example.com` (port 3000, Vite dev server, hot reload)
- **Prod:** `lawkitty.app` (port 8765, FastAPI serves built frontend)
- Both served via Cloudflare Tunnel (systemd user services).
- Deploy: merge to `master`, push, restart backend on production port.

## PostgreSQL
- Database: `lawkitty_cases` running in Docker container `lawkitty-postgres`.
- Dump core tables to `data/lawkitty_db_backup_core_<YYYYMMDD>.sql.gz` before major schema changes.
- Do not back up `chunks` table (pgvector embeddings, 9 GB) — can be regenerated.
- Restore: `gunzip -c <dump> | docker exec -i lawkitty-postgres psql -U postgres lawkitty_cases`

## Housekeeping
- Run `git prune` and `git remote prune origin` periodically to clean stale references.
- If `.gitignore` rules change, commit the fix — don't leave ignored files lingering in the index.
- Review open branches weekly. Merge or delete stale ones.

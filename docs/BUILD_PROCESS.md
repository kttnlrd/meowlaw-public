# Build → Test → Integrate → Verify

The working process for every code and data batch in this repo. Three roles, five gates, no batch
closes without evidence.

## Roles

**Master — deepseek-v4-pro (the orchestrator).** Holds the plan, splits it into batches, writes each
executor's packet, arbitrates disagreements, decides commit and rollback, reports to Harry. Never
edits corpus data directly and never accepts a claim it has not read the output of.

**Executor — deepseek-v4-flash (one subagent per batch).** Isolated context. Does the work in one
batch, returns files changed, exact commands run and their real output. No writes under `data/**`
unless the batch *is* a data batch and a rollback tag exists. No commits of data. Hard limit: one
batch, then report.

**Resolver — Claude Code on opus 5.5 (`claude -p --model claude-opus-5-5 --dangerously-skip-permissions`).**
Called when an executor fails the same check twice, when two verifications disagree, or when the
design is genuinely ambiguous. Gets the failing output verbatim, returns a decision or a patch.
Not a first responder — a tie-breaker and a second opinion.

## Gates

**1. Spec.** The batch is a set of ids from `docs/bug-plan-<date>.md`. Each carries a root cause
with `file:line` evidence that was actually read. No fix is planned against a guessed cause.

**2. Build.** Smallest change at the shared fix site. Several ids sharing a fix site are done in one
batch; ids that only look related are not. Fix sites live in the pipeline scripts, never in the
served artefact.

**3. Test.** The check is written **first** and shown **failing** against the current state, then
passing. A check that cannot fail is decoration — if it passes before the fix, the test is wrong or
the defect is not where we think. Every new defect class gets a detector in
`scripts/scan_corpus_error_classes.py`, registered in `main()`, plus a self-test.

**4. Integrate.** Data changes go in blocks with a verify pass that **re-derives** the change rather
than trusting line numbers. Rollback tag or file copy first, and it is named in the report.
Provenance: a derived artefact records what it was derived from; a producer may only delete rows it
produced. Commits are grouped by fix site, with the evidence (counts before/after, command) in the
message. If a route changed, restart the service; a restart is not a deployment.

**5. Verify.** End to end, by the master, against the running artefact — API response, MCP tool call,
rendered UI, byte counts on disk. The executor's summary is a claim, not a result. Then the corpus
audit and the readiness suite. The batch closes only when the tree is clean or the dirty files are
named and owned.

## Failure ladder

Executor fails a check → diagnoses and retries once with the diagnosis in hand → fails again →
opus resolver gets the verbatim failure and either fixes it or rules the approach wrong → if the
resolver and the executor disagree, the master decides and records why in the batch log.

## Batch log

`docs/batches/<batch-id>.md`, appended as the batch runs: the packet given, commands run, real
output, before/after counts, the check that failed first, the rollback point, the commit, and what
the master verified independently.

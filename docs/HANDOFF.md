## Handoff Card

- Goal: Add Strata inference server support (done); next, docs/code size cleanup.
- As-Of: 2026-10-05 11:42 UTC (2026-10-05 20:42 +0900)
- Branch/HEAD: master @ 767041d (before this handoff commit); not pushed
- Mode: FINISHED
- Working On: P-20260930-04
- Stop Point: P-20261005-01 committed (767041d) after independent review + doc-repair re-review (both PASS with minors).
- Next Step: Size cleanups in TODO Next order. Decide P-20261005-03 (lower-case "error" log level). Real Strata/GPU check is P-20261005-02. Push/deploy to dgx1 is the owner's call.
- Key Decisions:
  - Switcher drops the client `Host`; every backend now gets `Host: 127.0.0.1:<port>` (nginx-like), needed for Strata's DNS-rebinding guard (`docs/DECISIONS.md` § P-20261005-01).
  - `/health` 200 with JSON `"loaded": false` = still loading, checked before the `status` rule.
  - `[strata] ` lines: upper-case ERROR/FATAL/CRITICAL > WARNING/WARN > info.
- Key Files: `src/proxy.py` (`_forward_headers`), `src/health.py`, `src/process_manager.py`, `tests/test_strata_compat.py`, README § Strata; record `artifacts/P-20261005-01/plan.md` (+ `research-strata.md`, `review.md`, `review-repair.md`).
- Env Hints: `.venv` available, not active; Python 3.13.5.
- Critical Context: User forbids Co-Authored-By trailers. Reviewers must not run `git stash`/checkout in worktrees. Strata recipe tested against Strata commit 6f32ec0 only (fake server).
- Completed Since Last: P-20261005-01
- Commands: `timeout 300 .venv/bin/python -m pytest -q tests`; `timeout 60 .venv/bin/python -m compileall -q src tests`; `git diff --check`.
- Verification: full suite 1121 passed (executor + reviewer); after docs-only repair, strata compat 6 passed, diff --check clean.
- Risks/Blockers: Strata not run on real hardware; 3 s stop grace vs Strata's slower shutdown (P-20261005-02); earlier real-server gaps (P-20260929-02/-04, P-20260927-02, P-20260923-06).

## Handoff Card

- Goal: Restore the stats dashboard redesign for a user trial and resume the canonical queue.
- As-Of: 2026-09-25 13:50 UTC (2026-09-25 22:50 +0900)
- Branch/HEAD: master @ c180dde (revert of 516c985); redesign restored in the next commit
- Mode: FINISHED
- Working On: P-20260913-04
- Stop Point: `docs/TODO.md:5`; the `tests/test_proxy.py` split is unstarted.
- Next Step: Inspect the Working On ID and propose its plan for approval; DISCOVERED items require explicit promotion.
- Key Decisions:
  - P-20260925-01 (stats dashboard redesign, 516c985) was reverted by user request (c180dde), then restored unchanged for a user trial; `src/stats.py`, stats tests, README, and DECISIONS match 516c985. The redesign hides per-model token totals (generated/prefill/prompt/cached) from the page; they remain in `/stats/json` and the user wants them kept. The stats.py split (P-20260925-02) and redesign follow-ups (P-20260925-03) stay dropped pending the trial outcome.
  - Test/module splits stay move-only with AST/node-ID accounting under `artifacts/{PlanID}/outputs/` (P-20260913-03, P-20260918-04, P-20260923-02).
- Key Files: `tests/test_proxy.py` (split target); `src/stats.py` and `tests/test_stats_page.py` (redesign under trial); `docs/TODO.md` (queue).
- Env Hints: `.venv` available, not active; Python 3.13.5; Node v24 (used by a page-script test in `tests/test_stats_max_aggregate.py`); no GPU needed.
- Critical Context: Oversized file: `tests/test_proxy.py` (1243 lines, P-20260913-04); `src/stats.py` is 1136 lines again. Maintained suite is 679 tests.
- Completed Since Last: P-20260925-01 reverted, then restored for trial (user request).
- Commands: `timeout 120 .venv/bin/python -m pytest -q tests`; `timeout 30 .venv/bin/python -m compileall -q src tests`.
- Verification: 679 passed with the redesign restored on c180dde; restored files match 516c985 exactly.
- Risks/Blockers: User trial of the redesign pending (keep, drop, or add token totals back); real DGX shutdown unverified (P-20260923-06); root pytest discovery includes artifacts/ (P-20260923-05). No implementation blocker.

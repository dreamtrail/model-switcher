## Handoff Card

- Goal: Revert the stats dashboard redesign by user request and resume the canonical queue.
- As-Of: 2026-09-25 13:50 UTC (2026-09-25 22:50 +0900)
- Branch/HEAD: master @ baa1f17 (revert of 516c985 uncommitted in working tree)
- Mode: FINISHED
- Working On: P-20260913-04
- Stop Point: `docs/TODO.md:5`; the `tests/test_proxy.py` split is unstarted.
- Next Step: Inspect the Working On ID and propose its plan for approval; DISCOVERED items require explicit promotion.
- Key Decisions:
  - P-20260925-01 (stats dashboard redesign, 516c985) was reverted by user request (low value); `src/stats.py`, stats tests, and README match the pre-redesign state, and its `docs/DECISIONS.md` section was removed by the revert. The follow-on stats.py split (P-20260925-02) and redesign follow-ups (P-20260925-03) were dropped.
  - Test/module splits stay move-only with AST/node-ID accounting under `artifacts/{PlanID}/outputs/` (P-20260913-03, P-20260918-04, P-20260923-02).
- Key Files: `tests/test_proxy.py` (split target); `docs/TODO.md` (queue).
- Env Hints: `.venv` available, not active; Python 3.13.5; Node v24 (used by a page-script test in `tests/test_stats_max_aggregate.py`); no GPU needed.
- Critical Context: Oversized file: `tests/test_proxy.py` (1243 lines, P-20260913-04); `src/stats.py` is back to 814 lines. Maintained suite is 619 tests.
- Completed Since Last: P-20260925-01 reverted (user request).
- Commands: `timeout 120 .venv/bin/python -m pytest -q tests`; `timeout 30 .venv/bin/python -m compileall -q src tests`.
- Verification: 619 passed with the revert applied on baa1f17; compileall clean; git diff --check clean.
- Risks/Blockers: Revert not yet committed; real DGX shutdown unverified (P-20260923-06); root pytest discovery includes artifacts/ (P-20260923-05). No implementation blocker.

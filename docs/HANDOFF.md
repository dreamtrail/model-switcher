## Handoff Card

- Goal: Preserve completed P-20260918-05 context and resume P-20260918-03.
- As-Of: 2026-09-18 10:13 UTC (2026-09-18 10:13 Z)
- Branch/HEAD: master @ 2e09b19 (before this handoff commit)
- Mode: FINISHED
- Working On: P-20260918-03
- Stop Point: `docs/TODO.md:5` Next — P-20260918-03.
- Next Step: Run Discover and prepare the plan for the Working On ID.
- Key Decisions:
  - `Max Agg Gen t/s` is per-model, lifetime-until-restart, and rendered as aggregate throughput plus peak decode concurrency (for example, `107.8@3`).
  - Maximal positive-overlap cohorts use exact completion tokens over cohort wall-clock time; touching intervals remain separate.
  - Exact cohort semantics are preferred over caps or estimates, accepting delayed publication and interval-state growth during continuous overlap.
- Key Files: `src/swapper.py` (split target and request lifecycle), `tests/test_swapper_*.py` (behavioral coverage), `src/stats.py` (new cohort observer integration), `docs/DECISIONS.md` (rationale).
- Env Hints: `.venv` available; Python 3.13.5; Node.js is required for executable dashboard acceptance tests; automated tests require no GPU.
- Critical Context: P-20260918-05 is complete; size findings are already covered by P-20260918-03, P-20260918-04, P-20260913-03, and P-20260913-04, so no duplicate tasks were added.
- Completed Since Last: P-20260918-05
- Commands: `.venv/bin/python -m compileall -q src`; `.venv/bin/python -m pytest tests/test_stats.py --tb=short -q`; `.venv/bin/python -m pytest tests/ --tb=short -q`.
- Verification: compileall passed; 108/108 focused and 456/456 full tests passed; implementation, documentation, and test audits PASS.
- Risks/Blockers: no current blocker; a continuously overlapping decode cohort delays publication and can grow retained interval state until the cohort closes.

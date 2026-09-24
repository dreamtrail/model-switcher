## Handoff Card

- Goal: Preserve the completed control-lifecycle split and resume the canonical queue.
- As-Of: 2026-09-24 13:34 UTC (2026-09-24 13:34 Z)
- Branch/HEAD: master @ 0a7b6a5 (before this handoff commit)
- Mode: FINISHED
- Working On: P-20260913-04
- Stop Point: `docs/TODO.md:5`; the proxy test split is unstarted.
- Next Step: Inspect the Working On ID and propose its plan for approval; DISCOVERED items require explicit promotion.
- Key Decisions:
  - Test splits move class/helper regions byte-verbatim; AST-exact and node-ID accounting proves preservation, with evidence under `artifacts/{PlanID}/outputs/` (P-20260913-03, P-20260918-04, P-20260924-02).
  - Shared test helpers live in non-collected `tests/*_helpers.py` modules; the entry module keeps the original suite docstring.
- Key Files: `tests/test_proxy.py` (split target); `tests/helpers.py`, `tests/stats_helpers.py`, `tests/quick_switch_helpers.py`, `tests/control_helpers.py` (shared helper conventions); `artifacts/P-20260918-04/plan.md`, `artifacts/P-20260913-03/plan.md` (binding split conventions); `docs/TODO.md` (queue).
- Env Hints: `.venv` available, not active; Python 3.13.5; no GPU required for maintained tests.
- Critical Context: One oversized test file remains: `tests/test_proxy.py` (1243 lines / 46,091 bytes). Maintained suite is 619 tests; splits are move-only and must preserve that count. Prior split plans keep accounting evidence under `artifacts/{PlanID}/outputs/` (gitignored). Shutdown hooks, the control panel, and the pp-speed fix are complete and committed.
- Completed Since Last: P-20260924-02
- Commands: `timeout 120 .venv/bin/python -m pytest -q tests`; `timeout 30 .venv/bin/python -m compileall -q src tests`; `git diff --check`.
- Verification: 619 maintained tests passed at 0a7b6a5; split accounting 172/172 checks; compileall and git diff --check clean.
- Risks/Blockers: Real DGX shutdown unverified (P-20260923-06); root pytest discovery includes historical tests under ignored artifacts/ (P-20260923-05); combined shutdown-hook coverage gap (P-20260923-04). No implementation blocker.

## Handoff Card

- Goal: Preserve completed shutdown-hook support and resume the canonical queue.
- As-Of: 2026-09-23 13:13 UTC (2026-09-23 13:13 Z)
- Branch/HEAD: master @ d163af5 (before this handoff commit)
- Mode: FINISHED
- Working On: P-20260923-02
- Stop Point: `docs/TODO.md:5`; completed implementation and independent review are ready for this save commit.
- Next Step: Inspect the Working On ID and propose its plan for approval; DISCOVERED items require explicit promotion.
- Key Decisions:
  - Preserve string/`cmd` configs; optional `shutdown_cmd` runs locally with literal arguments and a 30-second default timeout.
  - Run the hook before local termination; always attempt local cleanup and block replacement until both stages succeed.
  - External agents own remote scripts; exit zero confirms cleanup by contract, not by independent remote observation.
- Key Files: `src/shutdown_hook.py` (hook ownership); `src/swapper_lifecycle.py` (cleanup integration); `tests/test_swapper_shutdown_hooks.py` (lifecycle coverage); `README.md`, `docs/SETUP.md` (script contract/budgets); `docs/TODO.md` (queue).
- Env Hints: `.venv` available, not active; Python 3.13.5; Node.js v24.15.0 for panel tests; no GPU required for maintained tests.
- Critical Context: Successful cleanup stages are retained across retries in memory only. No recovery guarantee across restart, SIGKILL, power loss, or network failure. Both oversized test files already have NEXT IDs.
- Completed Since Last: P-20260923-03
- Commands: `timeout 120 .venv/bin/python -m pytest -q tests`; `timeout 30 .venv/bin/python -m compileall -q src tests`; `git diff --check`.
- Verification: Executor reported 141 focused and 614 maintained-suite tests passed; compile passed. Fresh independent static review found no blocking defect; diff/AST checks passed. Tests were not independently rerun.
- Risks/Blockers: Real DGX shutdown remains unverified; combined cancellation/timeout/retry coverage and root pytest artifact discovery are recorded in DISCOVERED. No implementation blocker.

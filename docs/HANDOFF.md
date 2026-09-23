## Handoff Card

- Goal: Preserve the completed runtime-control-panel context and resume P-20260923-02.
- As-Of: 2026-09-23 03:53 UTC (2026-09-23 03:53 Z)
- Branch/HEAD: master @ 7f60757 (before this handoff commit)
- Mode: FINISHED
- Working On: P-20260923-02
- Stop Point: `docs/TODO.md:5` — P-20260923-02.
- Next Step: Run Discover and prepare the plan for the Working On ID.
- Key Decisions:
  - Serve the runtime-only control panel and API on the existing listener.
  - Trust direct local-network clients; the mutation header provides browser CSRF resistance, not authentication.
  - Preserve shared admission, fail-closed process-group cleanup, and one-model-at-a-time operation.
- Key Files: `tests/test_control_lifecycle.py` (next size-hygiene target); `src/control.py`, `src/swapper.py`, `src/swapper_lifecycle.py` (completed feature); `docs/TODO.md` (canonical queue).
- Env Hints: `.venv` available; Python 3.13.5; Node.js required by control-panel JavaScript tests; no GPU required.
- Critical Context: Runtime settings reset on restart; manual switches/unloads may interrupt in-flight requests. Size scan found two test files over 900 lines and both have NEXT IDs. `master` is five commits ahead of `origin/master` before this handoff commit.
- Completed Since Last: P-20260923-01
- Commands: `.venv/bin/python -m compileall -q src`; `.venv/bin/python -m pytest tests/test_control_api.py tests/test_control_lifecycle.py tests/test_control_panel.py --tb=short -q`; `.venv/bin/python -m pytest tests/ --tb=short -q`.
- Verification: compile passed; control tests passed 84/84; related tests passed 53/53; full suite passed 573/573; final implementation Audit and Audit Tests both PASS with zero Blocker/Major findings.
- Risks/Blockers: no blocker; the unauthenticated control API is appropriate only for the accepted trusted-local-network deployment, and local commits have not been pushed.

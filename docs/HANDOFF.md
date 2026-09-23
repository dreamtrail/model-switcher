## Handoff Card

- Goal: Preserve completed P-20260918-04 context and resume P-20260913-03.
- As-Of: 2026-09-23 00:41 UTC (2026-09-23 00:41 Z)
- Branch/HEAD: master @ 1b34938 (before this task commit)
- Mode: FINISHED
- Working On: P-20260913-03
- Stop Point: `docs/TODO.md:5` — P-20260913-03.
- Next Step: Run Discover and prepare the plan for the Working On ID.
- Key Decisions:
  - Delete the oversized quick-switch test monolith and replace it with five responsibility-focused collected modules plus one non-collected shared helper module.
  - Preserve all helper and test-method behavior exactly; use AST equality and normalized node IDs to verify the move rather than redesigning race/lifecycle setups.
  - Keep production files byte-identical and retain existing `src.swapper` monkeypatch and private seams.
- Key Files: `tests/quick_switch_helpers.py`, `tests/test_swapper_quick_switch_admission.py`, `tests/test_swapper_quick_switch_outcomes.py`, `tests/test_swapper_quick_switch_races.py`, `tests/test_swapper_quick_switch_cancellation.py`, `tests/test_swapper_quick_switch_shutdown.py`, `docs/TODO.md`.
- Env Hints: `.venv` available; Python 3.13.5; automated tests require no GPU.
- Critical Context: The original 2,000-line / 78,344-byte module is deleted. All six replacements are below 900 lines and 50,000 bytes; the support module is not collected by pytest.
- Completed Since Last: P-20260918-04
- Commands: focused five-module pytest run; `.venv/bin/python -m pytest tests/test_swapper_*.py --tb=short -q`; `.venv/bin/python -m pytest tests/ --tb=short -q`.
- Verification: nine helper and 26 test-method ASTs matched exactly; normalized collection matched 30/30; independent and aggregate split tests passed 30/30; related swapper suite passed 71/71; full suite passed 489/489; production hashes were unchanged; audit PASS with zero findings.
- Risks/Blockers: no blocker; pytest node module paths intentionally changed with the split, while class, method, parameter identities, assertions, and behavior remain unchanged.

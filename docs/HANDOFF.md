## Handoff Card

- Goal: Keep quick-switch lifecycle handling maintainable and ready for the next planned improvement.
- As-Of: 2026-09-18 06:04 UTC (2026-09-18 06:04 Z)
- Branch/HEAD: master @ f06faf1 (before this handoff commit)
- Mode: FINISHED
- Working On: P-20260918-03
- Stop Point: `docs/TODO.md:5` Next — P-20260918-03.
- Next Step: Run Discover and prepare the plan for the Working On ID.
- Key Decisions:
  - `--quick-switch` is CLI-only, defaults off, and leaves the flat YAML schema unchanged.
  - Quick switch off refuses different pending targets with 409; on uses latest-admitted-target wins with 409 for obsolete requests.
  - Process startup/cleanup outcomes are deterministic and replacement fails closed until tracked cleanup is confirmed.
- Key Files: `src/swapper.py` (lifecycle/admission), `src/process_manager.py` (teardown), `tests/test_swapper_quick_switch.py` (race contracts), `docs/DECISIONS.md` (rationale).
- Env Hints: `.venv` available; Python 3.13.5; automated tests require no GPU.
- Critical Context: Size hygiene added P-20260918-03 and P-20260918-04; existing large-file tasks remain canonical in TODO.
- Completed Since Last: P-20260918-02
- Commands: `.venv/bin/python -m compileall -q src`; focused CLI/swapper/process/stats suite; `.venv/bin/python -m pytest tests/ --tb=short -q`.
- Verification: 420/420 full, 211/211 focused, and 2/2 POSIX process-group tests passed; implementation and test audits PASS.
- Risks/Blockers: none current; deliberately detached groups and unobservable non-POSIX descendants remain outside cleanup guarantees.

## Handoff Card

- Goal: Keep model-switcher safe during slow model loads and ready for the next planned improvement.
- As-Of: 2026-09-18 06:00 UTC (2026-09-18 15:00 KST)
- Branch/HEAD: master @ 1cca01d (before the P-20260918-02 commit)
- Mode: FINISHED
- Working On: P-20260913-03
- Stop Point: `docs/TODO.md:5` Next — P-20260913-03.
- Next Step: Run Discover and prepare the plan for the Working On ID.
- Key Decisions:
  - `--quick-switch` is CLI-only, defaults off, and does not change the flat YAML schema.
  - With quick switch off, same-target requests join the pending load and different targets receive 409 `switch_in_progress`; with it on, the latest admitted target wins and obsolete requests receive 409 `swap_superseded`.
  - Process creation and cleanup fail deterministically as `model_start_failed` or `process_cleanup_failed`; replacement never starts before tracked cleanup is confirmed.
- Key Files: `src/swapper.py` (admission/lifecycle), `src/process_manager.py` (fail-closed teardown), `tests/test_swapper_quick_switch.py` (concurrency contracts), `docs/DECISIONS.md` (rationale).
- Env Hints: `.venv` available; Python 3.13.5; automated tests require no GPU.
- Critical Context: Supersession cancels readiness only; spawn and cleanup reach terminal states. Shutdown and external cancellation retain unresolved handles and block replacement when cleanup cannot be confirmed.
- Completed Since Last: P-20260918-02
- Commands: `.venv/bin/python -m compileall -q src`; focused CLI/swapper/process/stats suite; `.venv/bin/python -m pytest tests/ --tb=short -q`; explicit POSIX process-group checks.
- Verification: 420/420 tests passed; focused 211/211 passed; POSIX 2/2 passed without skips; implementation and test audits PASS.
- Risks/Blockers: none current; deliberately detached process groups and unobservable non-POSIX descendants remain outside the cleanup guarantee.

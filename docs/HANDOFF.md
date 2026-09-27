## Handoff Card

- Goal: TabbyAPI now runs as a backend behind the model switcher; next, resume the refactor queue.
- As-Of: 2026-09-27 14:34 UTC (2026-09-27 23:34 +0900)
- Branch/HEAD: master @ 10834f5 (before this handoff commit)
- Mode: FINISHED
- Working On: P-20260925-05
- Stop Point: `docs/TODO.md:5`; the `src/stats.py` split is unstarted.
- Next Step: Inspect P-20260925-05 and propose a move-only plan for approval; ask the user whether they can smoke-test TabbyAPI on real hardware (P-20260927-02).
- Key Decisions:
  - P-20260927-01: example recipe disables TabbyAPI auth (`--disable-auth true`) because TabbyAPI has no key flag/env var (keys only in `api_tokens.yml`); README documents the file option and the admin risk.
  - Two small generic changes: `_TABBY_LEVEL_RE` in `src/process_manager.py`; SSE events with `prompt_progress` and no output do not mark first token (`src/proxy.py`). See `docs/DECISIONS.md` § P-20260927-01.
- Key Files: `README.md` § TabbyAPI; `tests/test_tabbyapi_compat.py`; record `artifacts/P-20260927-01/plan.md`.
- Env Hints: `.venv` available, not active; Python 3.13.5; Node v24 (page tests).
- Critical Context: Behaviour pinned to TabbyAPI master `816c321`. Oversized: `src/stats.py` (P-20260925-05), `tests/test_proxy.py` now 1312 lines (P-20260913-04). Local master is ahead of upstream; push only when asked.
- Completed Since Last: P-20260927-01
- Commands: `timeout 300 .venv/bin/python -m pytest -q tests`; `timeout 60 .venv/bin/python -m compileall -q src tests`; `git diff --check`.
- Verification: 835 passed after the README fixes; independent review found no blocking issues (all 7 acceptance criteria pass).
- Risks/Blockers: TabbyAPI not run against a real server (P-20260927-02); P-20260926-01/-02 still lack independent review; real DGX shutdown unverified (P-20260923-06).

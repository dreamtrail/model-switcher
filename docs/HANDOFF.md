## Handoff Card

- Goal: The live generation rate on the stats dashboard now reflects real combined throughput; next, resume the refactor queue.
- As-Of: 2026-09-26 11:27 UTC (2026-09-26 20:27 +0900)
- Branch/HEAD: master @ 594ba1b (before this handoff commit)
- Mode: FINISHED
- Working On: P-20260925-05
- Stop Point: `docs/TODO.md:5`; the `src/stats.py` split is unstarted (file grew to 1532 lines / 53 KB).
- Next Step: Ask the user how the live tile looks under a real batch run; then inspect P-20260925-05 and propose a move-only plan (page HTML + CSS shared with `/control`) for approval.
- Key Decisions:
  - P-20260926-04: `rates.generation_tps.live` = (chunks in last 15 s × per-model tokens-per-chunk) / max(busy s, 1); busy = union of generating intervals (active ones to now) clipped to the window; ratio = Σ exact completion_tokens / Σ chunks over the last 50 successful streams (default 1.0); global uses union busy across models; 0 (`–`) when no decode is active. JSON adds `busy_s`, `tokens_per_chunk` (global null). See `docs/DECISIONS.md` § P-20260926-04.
  - User chose the 15 s window (`LIVE_WINDOW_S`) and kept the `–`-when-idle rule (flicker logged as P-20260926-05); exact `last_60s`/`last_300s` unchanged.
- Key Files: `src/stats.py` (`LIVE_WINDOW_S`, `_busy_seconds`, `_live_reports`, `record_gen_calibration`); `src/proxy.py` (`Outcome.gen_chunks` in `_process_sse_line`); `src/swapper.py` (calibration call); `tests/test_stats_live_rate.py`; plan/review record `artifacts/P-20260926-04/plan.md`.
- Env Hints: `.venv` available, not active; Python 3.13.5; Node v24 (page tests).
- Critical Context: Oversized files: `src/stats.py` (P-20260925-05), `tests/test_proxy.py` (P-20260913-04). Suite is 788 tests. 594ba1b is not pushed; push only when the user asks.
- Completed Since Last: P-20260926-04
- Commands: `timeout 180 .venv/bin/python -m pytest -q tests`; `timeout 30 .venv/bin/python -m compileall -q src tests`; `git diff --check`.
- Verification: 788 passed at 594ba1b; compileall and diff-check clean; two independent reviews (rev 1 and rev 2) found no defects; README drift fixed.
- Risks/Blockers: Not yet observed against a real backend under load; ratio can lag when chunk packing changes with load; an open but silent stream counts as busy; P-20260926-01/-02 still lack independent review; table overflow (P-20260926-03); real DGX shutdown unverified (P-20260923-06).

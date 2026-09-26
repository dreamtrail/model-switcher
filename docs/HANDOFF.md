## Handoff Card

- Goal: Stats dashboard gains a live (estimated) generation rate, and the control panel matches the stats look with links both ways; the refactor queue resumes next.
- As-Of: 2026-09-26 09:26 UTC (2026-09-26 18:26 +0900)
- Branch/HEAD: master @ 7deb3f0 (before this handoff commit)
- Mode: FINISHED
- Working On: P-20260925-05
- Stop Point: `docs/TODO.md:5`; the `src/stats.py` split is unstarted.
- Next Step: Get the user's feedback on the live rate and the restyled `/control`; then inspect P-20260925-05 and propose a move-only plan (now also covering the CSS shared by both pages) for approval.
- Key Decisions:
  - P-20260926-01: `rates.generation_tps.live` = streamed output chunks in the last 60 s / 60, `{"tps","window_s","estimate"}` per model + global (sum); a user-approved exception to exact counts, OpenAI `data:` streams only; page shows "≈", `–` for zero. See `docs/DECISIONS.md` § P-20260926-01.
  - P-20260926-02: `/control` copies the `/stats` palette, tiles, and cards (no shared stylesheet yet); header links `/stats`; API, element IDs, and 3 s refresh unchanged. See § P-20260926-02.
  - Stats tiles use a 2/4/8-column grid with a wide Loaded-model tile (seven tiles, no orphan row).
- Key Files: `src/proxy.py` (`_count_gen_chunks`); `src/stats.py` (`record_gen_chunks`, `STATS_HTML`); `src/control.py` (`CONTROL_HTML`); `tests/test_stats_live_rate.py`, `tests/test_proxy_gen_chunks.py`, `tests/test_control_panel.py`; previews under `artifacts/P-20260926-01/outputs/` and `artifacts/P-20260926-02/outputs/`.
- Env Hints: `.venv` available, not active; Python 3.13.5; Node v24 (page tests); `/usr/bin/google-chrome` for previews.
- Critical Context: Oversized files: `src/stats.py` (1401 lines, P-20260925-05), `tests/test_proxy.py` (1243 lines, P-20260913-04). Maintained suite is 762 tests. `origin/master` already matched 7deb3f0 before this commit (the executor did not push); push only when the user asks.
- Completed Since Last: P-20260926-01, P-20260926-02
- Commands: `timeout 180 .venv/bin/python -m pytest -q tests`; `timeout 30 .venv/bin/python -m compileall -q src tests`; `.venv/bin/python artifacts/P-20260926-02/outputs/make_previews.py`.
- Verification: 762 passed at 7deb3f0; compileall and `git diff --check` clean; light/dark/narrow previews reviewed. Both commits were made at the user's request without the planned independent review (executor checks only).
- Risks/Blockers: No independent review yet for P-20260926-01/-02; the live rate is an estimate (one chunk is not always one token); copied CSS can drift until P-20260925-05; table overflow edge case (P-20260926-03); real DGX shutdown unverified (P-20260923-06).

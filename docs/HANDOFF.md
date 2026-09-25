## Handoff Card

- Goal: Stats dashboard shows combined (busy-time) generation/prompt speed and token totals and links to the control panel; the user is trying it out, and the queue resumes afterwards.
- As-Of: 2026-09-25 15:28 UTC (2026-09-26 00:28 +0900)
- Branch/HEAD: master @ aef1f4a (before this handoff commit)
- Mode: FINISHED
- Working On: P-20260925-05
- Stop Point: `docs/TODO.md:5`; the `src/stats.py` split is unstarted (size hygiene promoted it from Discovered to the top of Next).
- Next Step: Confirm the user's trial feedback on the dashboard first; then inspect P-20260925-05 and propose a move-only plan for approval.
- Key Decisions:
  - P-20260925-04: `avg_aggregate` `{"tps","concurrency"}` under `rates.generation_tps` and `rates.prompt_tps` (per model + global) = tokens / busy wall time of closed overlap groups; idle gaps excluded; global adds per-model totals. Page columns Gen t/s, Gen total, Prompt t/s, Prompt total, TTFT, Cache, Gen tok, Prefill tok; Best gen t/s dropped from the page only (`max_aggregate` stays in JSON); width 80rem; header links `/control`. See `docs/DECISIONS.md` § P-20260925-04.
  - P-20260925-01 redesign was reverted (c180dde), then restored (2f6b887) at the user's request; it ships with P-20260925-04.
  - Splits stay move-only with AST/node-ID accounting under `artifacts/{PlanID}/outputs/`. For P-20260925-05, the stdlib-only import test (`tests/test_stats_contract.py`, AC10) blocks a plain `from src.stats_page import STATS_HTML` re-export; a draft approach is in `artifacts/P-20260925-02/plan.md`.
- Key Files: `src/stats.py` (collector + `STATS_HTML`); `src/swapper.py` (request-start/prefill hooks); `tests/test_stats_avg_aggregate.py`, `tests/test_stats_page.py`; `artifacts/P-20260925-04/outputs/make_previews.py` (headless Chrome previews).
- Env Hints: `.venv` available, not active; Python 3.13.5; Node v24 (page tests); `/usr/bin/google-chrome` for previews; no GPU needed.
- Critical Context: Oversized files: `src/stats.py` (1350 lines, P-20260925-05), `tests/test_proxy.py` (1243 lines, P-20260913-04). Maintained suite is 694 tests. `master` was 1 commit ahead of origin at aef1f4a; pushing is done only when the user asks.
- Completed Since Last: P-20260925-04
- Commands: `timeout 180 .venv/bin/python -m pytest -q tests`; `timeout 30 .venv/bin/python -m compileall -q src tests`; `.venv/bin/python artifacts/P-20260925-04/outputs/make_previews.py`.
- Verification: 694 passed at aef1f4a; compileall and `git diff --check` clean; independent review of P-20260925-04 found no significant defects (3000-case fuzz matched); the /control link was checked by the executor only.
- Risks/Blockers: User trial pending; the combined speeds do not update on a never-idle server until a gap occurs (documented); real DGX shutdown unverified (P-20260923-06); root pytest discovery includes artifacts/ (P-20260923-05).

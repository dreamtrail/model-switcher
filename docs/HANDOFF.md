## Handoff Card

- Goal: Add combined (busy-time) generation/prompt speed and token totals to the stats dashboard, then resume the canonical queue.
- As-Of: 2026-09-25 14:19 UTC (2026-09-25 23:19 +0900)
- Branch/HEAD: master @ 2f6b887 before the P-20260925-04 commit (redesign restored for trial)
- Mode: FINISHED
- Working On: P-20260913-04
- Stop Point: `docs/TODO.md:5`; the `tests/test_proxy.py` split is unstarted.
- Next Step: Inspect the Working On ID and propose its plan for approval; DISCOVERED items require explicit promotion.
- Key Decisions:
  - P-20260925-04: `rates.generation_tps.avg_aggregate` and `rates.prompt_tps.avg_aggregate` (`{"tps", "concurrency"}` or null, per model and global) are busy-time averages over closed overlap groups; idle gaps are excluded; global sums per-model totals. Prefill groups publish only when no in-flight request could still overlap (swapper hooks `record_request_start` / `record_prefill_end`). Page columns: Gen t/s, Gen total, Prompt t/s, Prompt total, TTFT, Cache, Gen tok, Prefill tok; Best gen t/s dropped from the page but `max_aggregate` kept in JSON; page max width 64rem → 80rem. See `docs/DECISIONS.md` § P-20260925-04.
  - P-20260925-01 (dashboard redesign) is committed again (2f6b887) and now ships together with P-20260925-04; both are pending the user's trial.
  - Test/module splits stay move-only with AST/node-ID accounting under `artifacts/{PlanID}/outputs/` (P-20260913-03, P-20260918-04, P-20260923-02).
- Key Files: `tests/test_proxy.py` (split target); `src/stats.py`, `tests/test_stats_avg_aggregate.py`, `tests/test_stats_page.py` (dashboard under trial); `docs/TODO.md` (queue).
- Env Hints: `.venv` available, not active; Python 3.13.5; Node v24 (required by `tests/test_stats_page.py`); no GPU needed.
- Critical Context: Oversized files: `tests/test_proxy.py` (1243 lines, P-20260913-04); `src/stats.py` is 1350 lines (split queued as P-20260925-05). Maintained suite is 693 tests.
- Completed Since Last: P-20260925-04 (combined gen/prompt speed, token columns, Best gen t/s removed from page, wider page).
- Commands: `timeout 120 .venv/bin/python -m pytest -q tests`; `timeout 30 .venv/bin/python -m compileall -q src tests`.
- Verification: 693 passed (stats subset 187); compileall clean; `git diff --check` clean; independent review found no significant defects; previews in `artifacts/P-20260925-04/outputs/` (all columns fit at 1280px).
- Risks/Blockers: User trial of the redesign + P-20260925-04 pending; `src/stats.py` size (P-20260925-05); combined speeds publish nothing on a never-idle server until a gap occurs (documented); real DGX shutdown unverified (P-20260923-06); root pytest discovery includes artifacts/ (P-20260923-05). No implementation blocker.

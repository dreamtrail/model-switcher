## Handoff Card

- Goal: Preserve the completed stats dashboard redesign and resume the canonical queue.
- As-Of: 2026-09-24 23:13 UTC (2026-09-25 08:13 +0900)
- Branch/HEAD: master @ 516c985 (before this handoff commit)
- Mode: FINISHED
- Working On: P-20260925-02
- Stop Point: `docs/TODO.md:5`; the `src/stats.py` split (added by size hygiene) is unstarted.
- Next Step: Inspect the Working On ID and propose its plan for approval; DISCOVERED items require explicit promotion.
- Key Decisions:
  - Stats redesign is page-only: `/stats/json` contract and `StatsCollector` stay unchanged; the page shows a subset (P-20260925-01, see `docs/DECISIONS.md`).
  - Page renders via createElement/textContent only; tests run the real embedded script in Node with a fake DOM (`tests/test_stats_page.py`).
  - Test/module splits stay move-only with AST/node-ID accounting under `artifacts/{PlanID}/outputs/` (P-20260913-03, P-20260918-04, P-20260923-02).
- Key Files: `src/stats.py` (collector + `STATS_HTML` at ~564-1136, split target); `tests/test_stats_page.py`; `artifacts/P-20260925-01/plan.md` and `outputs/make_previews.py` (headless Chrome preview/screenshot generator); `docs/TODO.md` (queue).
- Env Hints: `.venv` available, not active; Python 3.13.5; Node v24 (required by page tests); `/usr/bin/google-chrome` for headless screenshots; no GPU needed.
- Critical Context: Oversized files: `src/stats.py` (1136 lines) and `tests/test_proxy.py` (1243 lines, P-20260913-04). Maintained suite is 679 tests. Old stats tests in `tests/test_stats_max_aggregate.py` also extract the page script, so moving `STATS_HTML` must keep its import path working.
- Completed Since Last: P-20260925-01
- Commands: `timeout 120 .venv/bin/python -m pytest -q tests`; `timeout 30 .venv/bin/python -m compileall -q src tests`; `.venv/bin/python artifacts/P-20260925-01/outputs/make_previews.py`.
- Verification: 679 passed at 516c985; git diff --check clean; three independent reviews, no significant findings; screenshots checked (light/dark/fresh/narrow/long-name).
- Risks/Blockers: Stats page follow-ups open (P-20260925-03); real DGX shutdown unverified (P-20260923-06); root pytest discovery includes artifacts/ (P-20260923-05). No implementation blocker.

## Handoff Card

- Goal: More useful /stats dashboard (time split, busy time, loads); next, docs/code size cleanup.
- As-Of: 2026-09-30 01:49 UTC (2026-09-30 10:49 +0900)
- Branch/HEAD: master @ d9d7e5d (before this handoff commit); 5 commits ahead of upstream, not pushed
- Mode: FINISHED
- Working On: P-20260930-04
- Stop Point: `docs/TODO.md` Next; P-20260930-01/-02/-03 committed (83fd52e, 6840283, d9d7e5d), each independently reviewed.
- Next Step: Size cleanups in TODO Next order (P-20260930-04, P-20260929-05, P-20260925-05). Pushing and deploying to dgx1 (llm-server restart; also carries c3b7fc9) is the owner's call.
- Key Decisions:
  - Prompt share counts only requests with both prefill and decode samples; per-request sums (see `docs/DECISIONS.md` § P-20260930-01).
  - Busy time = wall-clock with ≥1 request in flight, from arrival incl. model-load wait, overlap once; separate from "Active now" (§ P-20260930-02).
  - Loads = every start → healthy attempt, recorded in `_ensure_model`; stop time separate and excluded from avg; failed vs abandoned by kind; tile line 2 "failures win"; `swaps` JSON kept (§ P-20260930-03).
- Key Files: `src/stats.py` (collector + embedded page), `src/swapper.py` (busy counter), `src/swapper_lifecycle.py` (`_ensure_model` / `_start_until_healthy`), `tests/test_stats_{time_split,utilization,loads}.py`; records `artifacts/P-20260930-0{1,2,3}/plan.md`.
- Env Hints: `.venv` available, not active; Python 3.13.5; headless Chrome at `/usr/bin/google-chrome` for page screenshots.
- Critical Context: User forbids Co-Authored-By trailers. Reviewers must not run `git stash`/checkout in worktrees (stash is shared with the main repo); use `git show rev:path` or a /tmp copy.
- Completed Since Last: P-20260930-01, P-20260930-02, P-20260930-03
- Commands: `timeout 300 .venv/bin/python -m pytest -q tests`; `timeout 60 .venv/bin/python -m compileall -q src tests`; `git diff --check`.
- Verification: 1078 passed; independent review + repair re-review PASS for all three.
- Risks/Blockers: Nothing checked against real model servers; review minors in P-20260930-05; earlier real-server gaps (P-20260929-02/-04, P-20260927-02, P-20260923-06).

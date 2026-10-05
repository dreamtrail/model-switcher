## Handoff Card

- Goal: TensorFold inference server support (done, plus stream-error log fix); next, docs/code size cleanup.
- As-Of: 2026-10-05 15:15 UTC (2026-10-06 00:15 +0900)
- Branch/HEAD: master @ 8437daf (before this handoff commit); 3 commits ahead of upstream, not pushed
- Mode: FINISHED
- Working On: P-20260930-04
- Stop Point: P-20261005-04 committed (740242d) and P-20261006-01 committed (8437daf), each after independent review (PASS with minors).
- Next Step: Size cleanups in TODO Next order. Push/deploy is the owner's call (user not yet asked to push). Real TensorFold/GPU check is P-20261005-05.
- Key Decisions:
  - TensorFold needs no health/proxy change: port opens only after load; `/health` 200 = ready.
  - Prompt time from `tensorfold.prefill_seconds` (MLX) / `prefill_s` (CUDA), same slot as `usage.prompt_time`; generation speed stays proxy-timed (`tokens_per_second`/`seconds` span unverified).
  - `[tensorfold] ` message starting `error:` / `request error:` / `stream error:` = error, `warning:` = warning; `... failed:` stays info (`docs/DECISIONS.md` § P-20261005-04).
- Key Files: `src/process_manager.py` (`_TENSORFOLD_*`), `src/prefill.py`, `tests/test_tensorfold_compat.py`, README § TensorFold, `config.example.yaml`; records `artifacts/P-20261005-04/` (plan, research, review), `artifacts/P-20261006-01/plan.md`.
- Env Hints: `.venv` available, not active; Python 3.13.5.
- Critical Context: User forbids Co-Authored-By trailers. Reviewers must not run `git stash`/checkout. TensorFold recipe pinned to TensorFold commit 609ca419 (fake server only).
- Completed Since Last: P-20261005-04, P-20261006-01
- Commands: `timeout 300 .venv/bin/python -m pytest -q tests`; `timeout 60 .venv/bin/python -m compileall -q src tests`; `git diff --check`.
- Verification: full suite 1166 passed at 740242d (before 8437daf); 8437daf: affected 110 passed, diff --check clean; full suite not re-run after 8437daf.
- Risks/Blockers: TensorFold and Strata not run on real hardware; 3 s stop grace vs MLX checkpoint save / Strata stop (P-20261005-05, P-20261005-02).

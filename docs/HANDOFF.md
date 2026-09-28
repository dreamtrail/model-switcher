## Handoff Card

- Goal: Accurate prompt and generation speed stats for llama.cpp, vLLM, SGLang, ExLlamaV3 (TabbyAPI); next, docs/code size cleanup.
- As-Of: 2026-09-28 23:53 UTC (2026-09-29 08:53 +0900)
- Branch/HEAD: master @ e42bc45 (before this handoff commit)
- Mode: FINISHED
- Working On: P-20260929-05
- Stop Point: `docs/TODO.md` Next; stats accuracy work committed (53723a0, e42bc45), not pushed.
- Next Step: Archive older `docs/DECISIONS.md` sections (P-20260929-05), then P-20260925-05; ask the user to run the real-server speed check (P-20260929-02/-04).
- Key Decisions:
  - Prompt/gen t/s prefer server-measured time (`timings`, TabbyAPI `usage.prompt_time`/`completion_time`, vLLM `metrics.*`); proxy timing is the fallback. See `docs/DECISIONS.md` § P-20260929-01, § P-20260929-03.
  - Unknown cached count: learn per model; skip until the model has ever reported one (`skipped_unknown_cache`). Single-chunk streams without server time are skipped (`skipped_buffered`).
  - Non-streaming requests count in speed figures when the server sends its own time; first token needs real output.
- Key Files: `src/prefill.py`, `src/decode.py`, `src/stats.py`, `src/proxy.py`, `README.md` § Accurate prompt speed; records `artifacts/P-20260929-01/plan.md`, `artifacts/P-20260929-03/plan.md`.
- Env Hints: `.venv` available, not active; Python 3.13.5; Node v24 (page tests).
- Critical Context: Backend shapes pinned to llama.cpp 1c47294, vLLM 6a6c295, SGLang 26d7539, tabbyAPI 816c321 / exllamav3 v1.5.2. vLLM needs `--enable-prompt-tokens-details --enable-per-request-metrics`; SGLang `--enable-cache-report`. TabbyAPI tool calls arrive in the final chunk. User forbids Co-Authored-By trailers.
- Completed Since Last: P-20260929-01, P-20260929-03
- Commands: `timeout 300 .venv/bin/python -m pytest -q tests`; `timeout 60 .venv/bin/python -m compileall -q src tests`; `git diff --check`.
- Verification: 985 passed; independent reviews passed AC1–AC7 for both plans, no significant findings.
- Risks/Blockers: No real-server run for any backend (P-20260929-02, P-20260929-04, P-20260927-02); real DGX shutdown unverified (P-20260923-06).

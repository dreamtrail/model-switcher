## Handoff Card

- Goal: Run TabbyAPI as a backend behind the model switcher, following the NInfer/SGLang/FreeToken compat pattern.
- As-Of: 2026-09-27 14:30 UTC (2026-09-27 23:30 +0900)
- Branch/HEAD: master @ 5678382 (before this handoff commit)
- Mode: IN-PROGRESS
- Working On: P-20260927-01
- Stop Point: Implementation done and uncommitted in the working tree; independent review was dispatched but its result was not received before save. Record: `artifacts/P-20260927-01/plan.md` § Independent review.
- Next Step: Rerun an independent read-only review of the uncommitted diff against the plan's acceptance criteria; fix findings if any; then commit `feat(tabbyapi): add backend compatibility` (approved with commit).
- Key Decisions:
  - Auth off in the example recipe (`--disable-auth true`): user rule "keep auth off if TabbyAPI has no pre-configured key like llama.cpp/vLLM"; TabbyAPI only reads keys from `api_tokens.yml` (no flag/env). README documents the file option and the admin-caller risk.
  - Two small generic src changes: `_TABBY_LEVEL_RE` (optional `HH:MM:SS.mmm`, uppercase loguru levels + `:`), and SSE events with `prompt_progress` and no output do not mark first token.
  - Behaviour pinned to TabbyAPI master `816c321` (source was cloned at `/tmp/tabby/tabbyAPI`, may be gone).
- Key Files: `src/process_manager.py`, `src/proxy.py` (`_process_sse_line`), `config.example.yaml`, `README.md` § TabbyAPI, `docs/DECISIONS.md` § P-20260927-01, `tests/test_tabbyapi_compat.py`, `tests/test_process_manager.py`, `tests/test_proxy.py`.
- Env Hints: `.venv` available, not active; Python 3.13.5; Node v24 (page tests).
- Critical Context: Uncommitted task files are intentionally left out of the handoff commit (commit only after review). Executor reported 835 passed (baseline 788), compileall and diff-check clean — not yet independently verified. Master is 1 commit ahead of upstream plus this handoff; push only when asked.
- Constraints & Preferences: Commit only after independent review; no pushing unless asked.
- Commands: `timeout 300 .venv/bin/python -m pytest -q tests`; `timeout 60 .venv/bin/python -m compileall -q src tests`; `git diff --check`.
- Verification: executor-only (see Critical Context).
- Risks/Blockers: Not run against a real TabbyAPI; `tests/test_proxy.py` grew to 1312 lines (P-20260913-04); `src/stats.py` still oversized (P-20260925-05).

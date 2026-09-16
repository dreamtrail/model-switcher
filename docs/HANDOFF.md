## Handoff Card

- Goal: Keep model-switcher compatible with local AI servers and ready for the next planned improvement.
- As-Of: 2026-09-16 02:18 UTC (2026-09-16 02:18 Z)
- Branch/HEAD: master @ c951893 (before this handoff commit)
- Mode: FINISHED
- Working On: P-20260913-03
- Stop Point: `docs/TODO.md` Next — P-20260913-03.
- Next Step: Run Discover and prepare the plan for the Working On ID.
- Key Decisions:
  - SGLang uses the generic foreground-process contract without an adapter, dependency, model rewriting, or native-endpoint support.
  - An implemented `/health` is authoritative; only original 404/405 permits `/v1/models` fallback, and the primary probe does not follow redirects.
  - SGLang config key, `--served-model-name`, and request `model` must match; Responses API passthrough remains version/model dependent.
- Key Files: `src/health.py` (readiness), `tests/test_sglang_compat.py` (v0.5.19 contract), `docs/DECISIONS.md` (rationale).
- Env Hints: `.venv` available; Python 3.13.5; automated tests require no GPU.
- Critical Context: SGLang v0.5.19 exposes `/v1/models` during warm-up while `/health` is 503; real accelerator/model smoke remains non-gating.
- Completed Since Last: P-20260916-01
- Commands: `.venv/bin/python -m compileall -q src`; targeted SGLang/health suite; `.venv/bin/python -m pytest tests/ --tb=short -q`.
- Verification: 380/380 tests passed; targeted 149/149 passed; implementation and test audits PASS.
- Risks/Blockers: none current.

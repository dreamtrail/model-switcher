## Handoff Card

- Goal: Honest streaming error handling in the switcher after the dgx1 frozen-client incident; next, docs/code size cleanup.
- As-Of: 2026-09-29 14:32 UTC (2026-09-29 23:32 +0900)
- Branch/HEAD: master @ c3b7fc9 (before this handoff commit); not pushed
- Mode: FINISHED
- Working On: P-20260929-05
- Stop Point: `docs/TODO.md` Next; P-20260929-06 committed (c3b7fc9), verified by independent review.
- Next Step: Archive older `docs/DECISIONS.md` sections (P-20260929-05), then P-20260925-05. Deploying c3b7fc9 to dgx1 is the owner's call (llm-server restart).
- Key Decisions:
  - Reviewed the ops proposal (`~/Dropbox/Hobby/server-setup/dgx/docs/PROPOSAL-switcher-stream-deadline.md`) instead of adopting it: its 64 KiB/50 B/s math and buffer-depth watchdog are wrong; with 3 userspace relays (server 1 → WireGuard → server 2 → user's relay on dgx1) the switcher cannot see a frozen pi, so write deadlines/TCP_USER_TIMEOUT would not have caught the incident.
  - User rejected duration (and by extension token) caps: they would interrupt legitimate long jobs. `P-20260827-01` no-total-deadline policy stands.
  - Client write failure after `prepare()` → stop, no error frame/`write_eof`/replacement 502; new error kind `client_disconnect` (checked before `stream_error`). See `docs/DECISIONS.md` § P-20260929-06.
- Key Files: `src/proxy.py` (`stream_proxy`), `src/swapper.py` (outcome kinds), `tests/test_proxy_client_disconnect.py`; record `artifacts/P-20260929-06/plan.md`.
- Env Hints: `.venv` available, not active; Python 3.13.5, aiohttp 3.14.1 (dgx1 runs 3.12.3 / aiohttp 3.14.3); Node v24.
- Critical Context: aiohttp `TestServer` forces `handler_cancellation=True`; production (`AppRunner` in `cli.py`) does not — disconnect tests use a local AppRunner helper. Stall detection for relayed clients belongs in pi or the server-1 relay (P-20260929-07). User forbids Co-Authored-By trailers.
- Completed Since Last: P-20260929-06
- Commands: `timeout 300 .venv/bin/python -m pytest -q tests`; `timeout 60 .venv/bin/python -m compileall -q src tests`; `git diff --check`.
- Verification: 989 passed; three independent reviews, final verified (AC1–AC6); new tests fail on old proxy.py.
- Risks/Blockers: Not run over the real pi/relay path or on dgx1; engine-side abort after upstream close unverified; no real-server stats checks (P-20260929-02/-04, P-20260927-02); DGX shutdown unverified (P-20260923-06).

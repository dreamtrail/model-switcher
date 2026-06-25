## Handoff Card

- As-Of: 2026-06-25 02:04 UTC (2026-06-25 02:04 Z)
- Branch/HEAD: master @ 81a7caa (post-task commit)
- Mode: FINISHED
- Working On: P-20260612-02
- Doc Anchors:
  - README.md — section: Output
  - docs/TODO.md — sections: Next, Done
  - docs/DECISIONS.md — section: Observability
- Commands:
  - .venv/bin/python -m pytest tests/test_process_manager.py tests/test_color_formatter.py --tb=short -q
- Stop Point: tests/test_swapper.py:1 — next edit starts here
- Verification: upstream log prefix change committed; related tests 71 passed
- Env Hints: .venv available; Python 3.13.5
- Completed Since Last: P-20260625-01
- Risks/Blockers: size hygiene still flags tests/test_swapper.py; tracked by P-20260612-02
- Source of Truth: docs/TODO.md

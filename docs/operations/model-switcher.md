# model-switcher operations

Goal: keep dgx1's switcher on the current master without interrupting inference.

Target: dgx1, systemd `llm-server` (checkout `/home/misuzu/src/model-switcher`, port 8000). vLLM runs in docker `spark-ds41f` on 4 nodes, outside the unit. Never `systemctl restart/stop llm-server` (its stop.sh stops the cluster). Safe reload: when `/stats/json` `concurrency.active` is 0, `sudo systemctl kill -s SIGKILL llm-server`; the unit respawns and start.sh adopts the running cluster on the first request. Earlier deploys: `artifacts/P-20261114-01/plan.md` (2026-11-14).

## Operations
- 2026-11-16 [P-20261116-05](../../artifacts/P-20261116-05/operation.md): push 6 commits; update dgx1 to a28efb6 (tab icon, Prompt tok column, stats split). Status: deployed a28efb6, verified; restart reset variant 1107 → native, replaced the cluster, one request got 502. Awaiting user decision on variant.

## Current state
dgx1 runs a28efb6 on variant native (:1030), as of 2026-11-16 10:15Z. Restarts reset the variant to the default (P-20261013-02): check the active variant before any restart.

## Next action
See the operation record.

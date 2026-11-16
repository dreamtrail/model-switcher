# model-switcher operations

Goal: keep dgx1's switcher on the current master without interrupting inference.

Target: dgx1, systemd `llm-server` (checkout `/home/misuzu/src/model-switcher`, port 8000). vLLM runs in docker `spark-ds41f` on 4 nodes, outside the unit. Never `systemctl restart/stop llm-server` (its stop.sh stops the cluster). Safe reload: when `/stats/json` `concurrency.active` is 0, `sudo systemctl kill -s SIGKILL llm-server`; the unit respawns and start.sh adopts the running cluster on the first request. Earlier deploys: `artifacts/P-20261114-01/plan.md` (2026-11-14).

## Operations
- 2026-11-16 [P-20261116-05](../../artifacts/P-20261116-05/operation.md): push 6 commits; update dgx1 to a28efb6 (tab icon, Prompt tok column, stats split). Status: deployed a28efb6, verified; restart reset variant 1107 → native, replaced the cluster, one request got 502. Awaiting user decision on variant.
- 2026-11-16 [P-20261116-09](../../artifacts/P-20261116-09/operation.md): push and deploy f7fc22b (variant memory, /stats polish); idle restart adopted the cluster; then switched to variant 1107 via the API (~3 min vLLM down, no failed requests). Status: done, independently verified.

## Current state
dgx1 runs f7fc22b on variant 1107 (:1107), as of 2026-11-16 10:52Z. The variant is now remembered in `config.state.json`, so a switcher restart comes back on 1107 and adopts the running cluster (on the first request).

## Next action
None pending.

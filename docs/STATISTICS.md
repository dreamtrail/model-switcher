# Runtime statistics

What the `/stats` dashboard and `/stats/json` report, how each speed and token number is measured, and which backend flags give exact numbers.

## Statistics

Model-switcher tracks runtime statistics in memory (reset on restart) and serves them at two switcher-local endpoints:

- `GET /stats` — human-readable dashboard that refreshes every 5 seconds and follows the system light/dark theme. The header links to the control panel (`/control`) and shows a live indicator: "Connecting…" before the first response, "Live · updated Ns ago" after each good response, and "Connection lost · showing last data" when a refresh fails (the last good data stays on screen), or "Connection lost · retrying" if no data has arrived yet. Seven stat tiles show the loaded model, average generation speed (tok/s), the live generation rate (**Live gen t/s**, with its source and window below the value, for example "engine · 5 s"; see `live` below), requests (with errors and requests in the last minute), active requests (with peak), model loads (**Loads**: the number of loads, the average load time, and on a second line the failed loads, for example "1 failed", or else the swaps, for example "3 swaps"; see `loads` below), and uptime (with the busy time and its share on two lines below it, for example "busy 1h 4m" and "33% of uptime"; see `utilization` below); large tile numbers are shortened (for example `12.3K`, `1.23M`) with the full number on hover. A **Models** card holds one compact table with a row per model plus an **All models** totals row (Reqs, Errors, Gen t/s, Live t/s, Gen total, Prompt t/s, Prompt total, PP %, TTFT p50 / p95, Cache hit %, Gen tok, Prefill tok, Prompt tok). **Gen total** and **Prompt total** show the combined speed while busy and the average number of requests running at once, for example `85.3 @2.4` (see `avg_aggregate` below); **Gen tok**, **Prefill tok**, and **Prompt tok** are the generated, uncached prompt, and all prompt (cached included) token totals, shortened like the tiles with the full number on hover; for example a 10,000-token prompt with 8,000 cached adds 2,000 to Prefill tok and 10,000 to Prompt tok. **PP %** (PP = prompt processing) is the part of request time spent on prompt processing rather than generation, for example `62%` (the rest is generation), with the seconds on hover such as "Prompt 123.4 s · Gen 75.6 s over 38 requests"; it shows `–` until a request has both times (see `time_split` below). A high share means prompt processing dominates that workload (long prompts, low cache hits); a low share means generation does. The loaded model is listed first with a green dot before its name (hover or a screen reader gives "Loaded"; other rows keep a blank space there so names line up), and other models are sorted by request count. An **Errors** card with one chip per error kind appears only when failures exist. The page intentionally shows a subset of the metrics; the full data (latency, bytes, cached token counts, starts/stops, windows, the per-model best cohort `max_aggregate`) is in `/stats/json`.
- `GET /stats/json` — the same data as JSON:

```json
{
  "uptime_seconds": 3600.5,
  "current_model": "llama-8b",
  "concurrency": {"active": 1, "peak": 3},
  "requests": {
    "total": 42, "success": 40, "failure": 2, "success_rate": 0.952,
    "errors": {"502": 1, "stream_error": 1},
    "latency_ms": {"count": 42, "avg": 120.5, "p50": 100.0, "p95": 300.0, "max": 320.0},
    "bytes_in": 1024, "bytes_out": 40960,
    "tokens": {"prompt": 8100, "completion": 2300, "cached": 512, "prefill": 7588, "cache_hit_rate": 0.063},
    "rates": {
      "prompt_tps": {"avg": 890.2, "avg_aggregate": {"tps": 1240.6, "concurrency": 1.4}, "server_timed_share": 0.8, "skipped_unknown_cache": 0},
      "generation_tps": {
        "avg": 38.1, "last_60s": 41.5, "last_300s": 39.0,
        "avg_aggregate": {"tps": 85.3, "concurrency": 2.4},
        "server_timed_share": 0.7, "skipped_buffered": 1,
        "live": {"tps": 84.6, "window_s": 5, "estimate": false, "source": "engine", "busy_s": 4.8, "tokens_per_chunk": null}
      }
    },
    "ttft_ms": {"count": 40, "avg": 210.0, "p50": 180.0, "p95": 640.0, "max": 700.0},
    "tpot_ms": {"count": 40, "avg": 26.3, "p50": 25.0, "p95": 30.0, "max": 31.0},
    "time_split": {"prefill_s": 123.4, "decode_s": 75.6, "prefill_share": 0.62, "generation_share": 0.38, "requests": 38}
  },
  "throughput": {"last_60s": 5, "last_300s": 12, "avg_rpm": 0.7},
  "swaps": {"count": 2, "avg_ms": 1500.0, "p95_ms": 2100.0, "max_ms": 2100.0},
  "loads": {
    "count": 3, "avg_ms": 1100.0, "p95_ms": 1600.0, "max_ms": 1600.0, "swaps": 2,
    "stop": {"count": 2, "avg_ms": 350.0, "p95_ms": 500.0, "max_ms": 500.0},
    "failed": {"swap_timeout": 1}, "abandoned": {"swap_superseded": 1}
  },
  "models": {
    "llama-8b": {
      "starts": 1,
      "stops": 0,
      "loads": {
        "count": 1, "avg_ms": 1600.0, "p95_ms": 1600.0, "max_ms": 1600.0, "swaps": 1,
        "stop": {"count": 1, "avg_ms": 500.0, "p95_ms": 500.0, "max_ms": 500.0},
        "failed": {}, "abandoned": {}
      },
      "requests": {
        "rates": {
          "generation_tps": {
            "avg": 38.1,
            "last_60s": 41.5,
            "last_300s": 39.0,
            "avg_aggregate": {"tps": 85.3, "concurrency": 2.4},
            "max_aggregate": {"tps": 107.8, "concurrency": 3},
            "live": {"tps": 84.6, "window_s": 5, "estimate": false, "source": "engine", "busy_s": 4.8, "tokens_per_chunk": 1.8}
          }
        }
      }
    }
  },
  "utilization": {"busy_seconds": 1200.2, "busy_share": 0.333}
}
```

Metric semantics:

- Counted requests are proxied requests plus rejected ones; `GET /v1/models`, `GET /models`, and the stats endpoints themselves are excluded.
- Success means the final response status is below 400 with no mid-stream error. Failures are keyed by kind: rejection kinds (`"400"`, `"404"`, `"503"`, `"504"`), upstream statuses (`"500"`, `"502"`, ...), mid-stream SSE errors (`"stream_error"`), client disconnects (`"client_disconnect"`: the client went away mid-stream; the upstream request is closed), and exceptions (`"error"`). Null fields (`success_rate`, `latency_ms`, swap and load durations) mean "no data yet".
- Latency is the upstream round-trip only: it excludes swap-queue wait and model-load time, so a request that triggers a cold start or swap reports its post-load latency.
- `utilization` (P-20260930-02) is the utilized time: `busy_seconds` is the wall-clock time with at least one request in progress, from arrival to finish, including waiting in the queue and waiting for a model to load; overlapping requests count once. It includes the current busy period. `busy_share` is `busy_seconds` divided by the switcher's own run time, measured with a monotonic clock (0–1; `null` only when that run time is 0), so after a system clock change it may differ slightly from `busy_seconds ÷ uptime_seconds`. Requests rejected as malformed (`400`) or for an unknown model (`404`) do not count; model loads with no waiting request (for example a manual load from the control panel) do not count either. It measures request time, not GPU compute time, and like the other stats it is kept in memory and resets on restart. The Uptime tile shows it under the uptime on two lines, "busy <time>" and "<N>% of uptime" (the second line is left out when the share is `null`; hover for the definition), and falls back to "since start" when the value is missing. See [docs/decisions/stats.md](decisions/stats.md#p-20260930-02--utilized-busy-time) § P-20260930-02.
- `swaps` counts only successful stop-then-start loads (a previous model process was stopped first, including same-model restarts after a crash) that ended with a healthy model; its duration covers stop + start + health check. It is kept unchanged for existing consumers; the page uses `loads`.
- `loads` (P-20260930-03, global and per model under `models[name].loads`) counts every model load: one process start attempt, whatever triggered it (first request, request after an idle or manual unload, swap, control-panel load, crash recovery). `count`, `avg_ms`, `p95_ms`, and `max_ms` cover successful loads only and time them from process start until the health check passes (`null` when `count` is 0). `swaps` is how many of those loads stopped a previous model first (globally it matches `swaps.count`), and `stop` times that stop (shutdown hook + process stop) separately for those successful loads; the stop is not part of the load time. Started loads that never became healthy are counted by kind and never timed: `failed` (`model_start_failed`, `swap_timeout`, `process_cleanup_failed`) and `abandoned` (`swap_superseded`: a newer model was requested; `shutting_down`; `cancelled`: the request that owned the load went away). A request for a model that is already loaded and healthy is not a load, and a failure while stopping the previous model (before any start) is not counted as a load. A failed load creates the model's per-model entry, so a model whose only load failed (for example a control-panel load that could not start) appears as a row with 0 requests. The **Loads** tile shows the count, "avg <time>", and a second line: "<N> failed" (all failed kinds added up) when any load failed, otherwise "<N> swaps" ("1 swap") when there were swaps; the line is left out when both are 0. Hover for the full picture: the definition, the swap and failed counts (each only when above 0), and the average stop time of the previous model, which is not included in the average. See [docs/decisions/stats.md](decisions/stats.md#p-20260930-03--loads-replace-swaps-on-the-page) § P-20260930-03.

Token metrics (when the upstream reports an OpenAI `usage` field):

- Token counters are exact counts read from `usage`: `prompt`, `completion`, and `cached` (from `prompt_tokens_details.cached_tokens` when present, else the llama.cpp/TabbyAPI `timings.cache_n`). Counters are recorded whenever usage is available, regardless of request status; there is no estimation (the only estimate is the labelled `live` rate below) — when usage is absent, the fields show no data.
- `prefill` is the uncached prompt work behind `rates.prompt_tps` — the server's `timings.prompt_n` when present, else `prompt − cached` — summed only for requests with a prefill time (see [Accurate prompt and generation speed](#accurate-prompt-and-generation-speed)).
- `cache_hit_rate` is the token-weighted lifetime ratio `cached ÷ prompt` (per model and global); it is `null` when no prompt tokens were recorded. Like the counters, it is derived from exact reported counts — no estimation. Backends that report cumulative cached counts may yield a ratio above 1; the value is reported as-is.
- For streaming requests the switcher transparently adds `"stream_options": {"include_usage": true}` to chat/completions requests (`/v1/chat/completions`, `/v1/completions`) whose clients did not set `stream_options`, so token metrics are available even when the client doesn't ask for them. Client-set values are forwarded untouched; non-JSON and non-streaming bodies pass through unchanged.
- `ttft_ms` is streaming-only; `tpot_ms`, `rates.generation_tps` (except the windows), and the generation aggregates also cover non-streaming requests when the server reports its own decode time (see [Accurate prompt and generation speed](#accurate-prompt-and-generation-speed)):
  - `ttft_ms` (time to first token) — from proxy start to the first chunk with generated output (content, reasoning, tool calls, or completions text; role-only, empty, and prefill-progress chunks do not count); captures queueing, prompt processing, and model behavior. Percentiles, not averages.
  - `tpot_ms` (time per output token) — decode time divided by `(tokens - 1)` for the same decode sample as `rates.generation_tps`; requires at least 2 tokens.
  - `rates.generation_tps` — generated tokens per second of decode time (token-weighted), using the server's own decode time when available and the switcher's first-chunk → end time otherwise; the trailing `last_60s`/`last_300s` windows are completion tokens ÷ window length for every request. `server_timed_share` is the fraction of decode tokens timed by the server (`null` before any) and `skipped_buffered` counts streams left out because all their output arrived in one chunk with no server decode time; `rates.prompt_tps` — uncached prompt tokens per second of prefill time, using the server's own prefill time when available and time-to-first-token otherwise (see [Accurate prompt and generation speed](#accurate-prompt-and-generation-speed)). The two rates are never blended.
  - Per-model `rates.generation_tps.max_aggregate` is separate from those average and rolling rates. It retains the highest completed decode cohort as `{"tps": number, "concurrency": integer}` until restart; it is not added to the global generation-rate object.
  - A cohort is a maximal connected group of positively overlapping decode intervals. A switcher-timed interval runs from a stream's first output chunk to its end; a server-timed one is the server's decode time ending at the response end (clamped to the request start), so non-streaming requests take part too. Its aggregate rate is the sum of the requests' decode tokens divided by the cohort's wall-clock span; concurrency is the peak number of simultaneously decoding eligible requests. Merely touching intervals are separate.
  - Only successful requests with a generation-speed sample (see `rates.generation_tps`) and a positive decode interval contribute. A cohort remains unpublished while any in-flight request of that model that started before the cohort ended could still add an overlapping interval; failed requests are released when they end and never join a cohort. The dashboard no longer shows this value (P-20260925-04); it stays in the JSON.
  - `rates.generation_tps.avg_aggregate` (per model and global) is the busy-time average over all completed cohorts: `tps` = total cohort tokens ÷ total cohort span, and `concurrency` = total per-stream decode time ÷ total cohort span (the average number of streams decoding at once). Only time with at least one eligible stream decoding counts, so idle gaps do not lower it. One stream alone gives `concurrency` 1.0 and the same `tps` as that stream. It is `null` until a cohort closes. The global value sums the per-model totals: busy time on different models is added, not merged into one timeline, which is fine because only one model is served at a time.
  - `rates.prompt_tps.avg_aggregate` is the same busy-time average for prompt processing. Each request's prefill interval runs from proxy start to its first content chunk (its TTFT) and carries its uncached prompt tokens; with a server prefill time it is that time ending at the first token (streaming) or starting at the request start (non-streaming, capped at the response end). Eligibility follows `prompt_tps.avg` plus a positive prefill interval. The global value sums the per-model totals in the same way. Overlapping intervals form groups; a group is published only when no in-flight request of that model could still overlap it (one that started before the group ended and has no first token yet, or whose first token came after the group started). Requests that fail or never produce a first token are released and never hold a group once they end.
  - The dashboard renders both as `<tps> @<concurrency>` with one-decimal concurrency — **Gen total** with one-decimal tps (for example `85.3 @2.4`) and **Prompt total** as a whole number like **Prompt t/s** (for example `2,140 @1.8`) — and `–` before any group is published. Like `max_aggregate`, a server that is never idle publishes nothing until a gap occurs.
  - `rates.generation_tps.live` (per model and global) is the combined generation speed over the last 5 seconds, reported as `{"tps": number, "window_s": 5, "estimate": bool, "source": "engine" | "estimate", "busy_s": number, "tokens_per_chunk": number}`. `tps` is `0` while nothing is generating (also while a prompt is still being processed), and the dashboard then shows `–`. Busy time (`busy_s`) is how long the model was generating in the window, with overlapping streams counted once (floor 1 s). The value comes from one of two sources:
    - `"engine"` (vLLM): while a reply is generating, the switcher reads the backend's `/metrics` about once per second and divides the rise of `vllm:generation_tokens_total` by the busy time. This counts tokens even when a stream sends nothing for a while (for example a buffered tool call). The poll goes straight to the model process with a 1 s timeout, never delays requests, and stops when generation stops; a timeout or error is retried, while a missing endpoint or counter switches that model process to the estimate. SGLang and llama.cpp counters update only when a request finishes, so those backends use the estimate (P-20261114-01).
    - `"estimate"` (all other cases, and the first second of a reply): the one exception to the exact-counts rule (P-20260926-01, P-20260926-04). Calibrated tokens are streamed chunks × the model's tokens per chunk, learned from the exact `completion_tokens` of its last 50 finished streams (`1.0` before any). A chunk is one choice in an OpenAI-style `data:` event whose delta carries `content`, `reasoning_content`, `reasoning`, or `tool_calls` (or `text` for `/v1/completions`). The ratio lags when chunk packing changes, and an open but silent stream counts as busy. Anthropic `/v1/messages` and `/v1/responses` named-event streams and non-streaming requests are not counted, and streams whose output arrived in a single chunk are not used for calibration.
    - Global: the loaded model's engine rate when it has one and no other model is generating; otherwise all models' calibrated tokens divided by their combined busy time. The global `tokens_per_chunk` is `null`. Unlike `last_60s`, which counts exact tokens only when a request ends, the live rate moves while a stream is running. Details: [docs/decisions/stats.md](decisions/stats.md#p-20261114-01--live-generation-rate-engine-counter-and-5-s-window) § P-20261114-01 (engine source) and [§ P-20260926-04](decisions/stats.md#p-20260926-04--live-generation-rate-busy-time-and-calibration) (busy time and calibration).
- `time_split` (per model and global, P-20260930-01) shows how request time divides between prompt processing and generation: `prefill_s` and `decode_s` are the summed prefill and decode seconds from the subset of **Prompt t/s** and **Gen t/s** samples whose requests have both times known (so usually smaller than the totals behind those speeds), `prefill_share` and `generation_share` are fractions of their sum (0–1, adding up to 1; `null` before any counted request), and `requests` is how many requests were counted. Only successful requests with **both** a prefill and a decode sample count, so both sides cover the same requests; requests left out of either speed (unknown cache, buffered stream, non-streaming without server times, fully cached prompts, failures) are left out of both. Each request adds its own seconds, so overlapping requests each count. SGLang prefill time is the switcher's time to first token and includes queueing; swap and model-load time is not included. The global value is kept alongside the per-model values and equals their sum. See [docs/decisions/stats.md](decisions/stats.md#p-20260930-01--prompt-vs-generation-time-split) § P-20260930-01.
- Non-streaming requests contribute token counts, plus prompt speed and generation speed (with **Gen total** and `max_aggregate`) when the server reports its own prefill or decode time; the switcher cannot split a single whole-body response into prompt and generation time by itself.

### Accurate prompt and generation speed

**Prompt t/s** (`rates.prompt_tps`) uses the best prefill time each response offers, in this order (P-20260929-01):

1. `timings.prompt_n` ÷ `timings.prompt_ms` (llama.cpp, TabbyAPI, Strata) — the server's own count of new prompt tokens and prefill time. Splash's `prompt_n` includes cached tokens, so for Splash the count is `metrics.prefill.tokens` (from the later usage chunk) instead.
2. `prompt − cached` ÷ `usage.prompt_time` (TabbyAPI), or ÷ `tensorfold.prefill_seconds` / `tensorfold.prefill_s` (TensorFold MLX / CUDA).
3. `prompt − cached` ÷ `metrics.time_to_first_token_ms` (vLLM).
4. `prompt − cached` ÷ the switcher's time to first token (streaming only). This also includes queueing, tokenization, the first decode step, and network time, so it reads lower than the server's own number.

`cached` comes from `usage.prompt_tokens_details.cached_tokens`, else `timings.cache_n`. For options 2–4, a missing cached count means 0 only if the model has reported one before; a model that has never reported one is left out of **Prompt t/s**, **Prompt total**, and **Prefill tok** (otherwise a fully cached prompt would look sky-high), and the request is counted in `rates.prompt_tps.skipped_unknown_cache`. `rates.prompt_tps.server_timed_share` is the fraction of prefill tokens timed by the server (`null` before any). Start the backend with these flags for exact numbers:

| Backend | Flags |
|---|---|
| llama.cpp | none |
| TabbyAPI (ExLlamaV3) | none |
| vLLM | `--enable-prompt-tokens-details --enable-per-request-metrics` |
| SGLang | `--enable-cache-report` (SGLang reports no prefill time, so the rate uses time to first token) |
| Strata | none |
| TensorFold | none |
| Splash | none |

**Gen t/s** (`rates.generation_tps.avg`, plus `tpot_ms`, **Gen total**, and `max_aggregate`) likewise uses the server's own decode time first, for streaming and non-streaming requests alike (P-20260929-03):

1. `timings.predicted_n` ÷ `timings.predicted_ms` (llama.cpp, TabbyAPI, Strata). For Splash the count is `metrics.decode.tokens` instead, which leaves out the first output that `predicted_ms` does not cover.
2. `completion_tokens` ÷ `usage.completion_time` (TabbyAPI).
3. `completion_tokens` ÷ `metrics.generation_time_ms` (vLLM).
4. `completion_tokens` ÷ the switcher's time from the first output chunk to the stream end (streaming only), used only when the output arrived in at least two chunks. A stream whose whole output came in one chunk — for example a TabbyAPI tool call, which is held back and sent in the final chunk — would otherwise look impossibly fast, so it is left out and counted in `rates.generation_tps.skipped_buffered`.

Speed is tokens ÷ time (N tokens, not N − 1). Flags for exact generation speed:

| Backend | Flags |
|---|---|
| llama.cpp | none |
| TabbyAPI (ExLlamaV3) | none |
| vLLM | `--enable-per-request-metrics` |
| SGLang | none available: streaming uses the switcher's own timing; non-streaming generation is not timed |
| Strata | none |
| TensorFold | none available: streaming uses the switcher's own timing; non-streaming generation is not timed |
| Splash | none |

Known limitations: these paths shadow upstreams serving the same paths (like `/v1/models`); latency samples are unbounded in memory (fine for a local dev tool); requests naming unknown models each create a per-model stats row keyed by the client-supplied name (a client-sent variant id such as `name@variant` is counted only in the global errors, not in that variant's row); the pending SSE line buffer is capped (~64 KiB) — once the cap is hit, non-line-shaped content (e.g. a binary audio stream without newlines) is discarded, so token metrics may be absent for such streams (byte counts remain exact).

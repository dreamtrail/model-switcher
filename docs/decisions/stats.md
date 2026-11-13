# Statistics and Dashboard Decisions

Complete statistics, dashboard, and matching control-panel presentation records since P-20260925, in their original relative order. Superseded definitions remain as history.

Return to the [decision index](../DECISIONS.md). Earlier metric contracts, including the P-20260913-02 rule called "above" in P-20260926-01, are in the [archive](archive.md#p-20260913-01--p-20260913-02--statistics--token-metrics).

## P-20260925-01 — Simplified Stats Dashboard

### Scope
- **Change the page only** — only the embedded `STATS_HTML` in `src/stats.py` changed. `StatsCollector`, `snapshot()`, and the `/stats/json` contract are unchanged, so JSON consumers are unaffected.

### Layout (revision 1; superseded by Revision 2 below)
- **Use one status line, one compact table, and one errors line** — the seven summary cards and two wide tables (23 columns) were replaced by a status line (loaded model, uptime, active/peak, swaps with average time in seconds, requests per minute), an 8-column table (Model, Reqs, Errors, Gen t/s, Best gen t/s, Prompt t/s, TTFT p50 / p95, Cache), and an errors line shown only when failures exist. *Superseded by Revision 2: the status line became six stat tiles and the errors line became an Errors card with chips.*
- **Put the loaded model first** — the loaded model's row is highlighted and listed first; other models are sorted by request count (ties by name). A final **All** row shows global totals; Best gen t/s is always `–` there because the global block has no `max_aggregate`. *Superseded by Revision 2: the **All** row became an **All models** totals row in `<tfoot>`; ordering is unchanged.*
- **Keep rendering safe and null-tolerant** — text-only DOM updates (no `innerHTML`), `–` for null, missing, or non-finite values, thousands separators for integers, 5-second refresh, and keep-last-render on fetch failure. No new dependencies.

### Dropped From the Page (Still in JSON)
- Request latency (avg/p50/p95/max), bytes in/out, raw token counts (prompt/completion/cached/prefill), process starts/stops, success rate, and `avg_rpm`. These are low-value at a glance or duplicate what the kept columns show; the user chose to keep speed, cache, response time (TTFT), and health/errors on the page.

### Revision 2 — Visual Polish
- **Same data, calmer presentation** — requested as "make it more aesthetically appealing". Metrics, table formatting, ordering, and the JSON contract are unchanged; only the `STATS_HTML` markup, CSS, and render code changed. Formatting changes are limited to the tiles: "req/min" is shown as "N in last min" under Requests, and large tile numbers are compacted (`1,234`, `12.3K`, `1.23M`) with the full number in the tooltip; table cells keep full numbers.
- **Palette via CSS custom properties** — page, surface, ink, hairline, baseline, accent, meter track, and status colors (good/serious/critical) are variables in `:root`, with dark values under `@media (prefers-color-scheme: dark)`. No categorical series colors are used: one accent plus a fixed status palette.
- **Stat tiles instead of a status line** — six tiles (Loaded model, Generation, Requests, Active now, Swaps, Uptime), each a label, a large value, and a small muted sub-line. Tiles use a fixed 6/3/2-column grid by width so they never leave a single orphan tile on its own row.
- **Status is dot + label, never color alone** — the header live indicator ("Connecting…", "Live · updated Ns ago", "Connection lost · showing last data" or "· retrying" before any data; a 200 response whose body is not a JSON object counts as a failure), the "Loaded" badge, error counts, and error chips all pair a colored dot with text.
- **Errors card instead of an errors line** — the rev-1 errors line became an **Errors** card with one chip (dot, kind, count) per error kind, shown only when failures exist.
- **Table stays the single detail view** — same 8 columns; the totals row is now "All models" in `<tfoot>` (separate border model so its top rule renders); Cache adds a small decorative meter (`aria-hidden`, width clamped to 0–100%, none when the rate is missing). Tabular figures are used only in the table.

### Status
- Implemented under P-20260925-01 with an executable Node-based page test (`tests/test_stats_page.py`).

## P-20260925-04 — Average Aggregate Speed and Token Totals

### Metric Definitions
- **Busy-time average, not wall-clock average** — `rates.generation_tps.avg_aggregate` is total cohort completion tokens divided by total cohort span, where a cohort is a connected group of overlapping eligible decode intervals (the same cohorts as `max_aggregate`). Only time with at least one eligible stream decoding counts; idle gaps between cohorts are excluded, so a quiet server does not look slow. `concurrency` is total per-stream decode time divided by the same busy time: the average number of streams decoding at once. A single stream gives concurrency 1.0 and the stream's own speed.
- **Same definition for prompt processing** — `rates.prompt_tps.avg_aggregate` uses one prefill interval per request, from proxy start to the first content chunk (`[first_token_at − ttft, first_token_at)`), carrying the uncached prompt tokens (`prompt − cached`, or `prompt` when cached is unknown). Eligibility follows `prompt_tps.avg` (successful streaming requests with a first token and at least one uncached prompt token) plus a positive prefill interval.
- **Global = sum of per-model totals** — the global block adds each model's tokens, busy time, and stream time. Busy time on different models is added, not merged into one timeline; this is fine because only one model is served at a time. (The plan's wording, "sums of per-model accumulators", is implemented literally.)
- **Shape** — `{"tps": number, "concurrency": number}` or `null` before any group closes; additive to the JSON contract, present per model and globally.

### Publication Rule
- **Publish only closed groups** — decode cohorts follow the existing `max_aggregate` rule (held while an active decode started before the cohort ended). A prefill group `[start, end)` is held while any in-flight request of that model started before `end` and has no first token yet or got its first token after `start`; such a request could still add an overlapping interval. Requests whose prefill ended before the group started, or that started at or after its end, do not hold it.
- **Hook** — the swapper records each proxied request's start (`record_request_start`) and closes it in the same `finally` block that closes the decode (`record_prefill_end`), sharing the request identity used for decode start/end. Every started request is released whether it succeeds, fails, or never produces a first token, so no pending state is left behind.
- **Accepted limitation** — while overlap continues (a never-idle server), nothing new is published until a gap occurs, the same as `max_aggregate`.

### Page
- **Columns** — Model, Reqs, Errors, Gen t/s, Gen total, Prompt t/s, Prompt total, TTFT p50 / p95, Cache, Gen tok, Prefill tok. The two "total" headers carry the tooltip "Combined speed while busy @ average concurrent requests". Gen total renders `85.3 @2.4` (one-decimal tps); Prompt total renders like Prompt t/s (whole number with thousands separators) plus one-decimal concurrency, e.g. `2,140 @1.8`; `–` when null or malformed. The All models row uses the global values.
- **Token totals return to the page** — Gen tok (`tokens.completion`) and Prefill tok (`tokens.prefill`) per model and in the All models row, shortened like the tiles (`45.2K`, `1.23M`) with the full number on hover. Reqs keeps full numbers.
- **Wider page** — the page max width grew from 64rem to 80rem so all 11 columns fit on a typical desktop (user choice); narrow screens still scroll the table sideways.
- **Best gen t/s dropped from the page, kept in JSON** — the column and its formatter were removed (user revision 2); per-model `max_aggregate` stays in `/stats/json` unchanged because the JSON contract is additive-only.

### Status
- Implemented under P-20260925-04 (rev 2) with collector unit tests, a real-stack JSON test, and Node page tests.

## P-20260926-01 — Live (Estimated) Generation Rate

### Approved Exception to "Exact Token Counts Only"
- **User-approved estimate** — the user chose to count streamed chunks and show an estimate marked "≈" (2026-09-26). This is a deliberate, limited exception to the exact-counts rule in P-20260913-02 above; every other token metric stays exact, and chunk counts never feed `tokens`, `last_60s`/`last_300s`, `avg`, or the aggregates.
- **Why** — exact `usage` arrives only when a stream ends, so exact windows jump at request end and show nothing while a long stream runs. Counting chunks is the only cheap in-flight signal.

### Definition
- **Unit** — one per choice in an OpenAI-style SSE `data:` event whose delta has non-empty `content`, `reasoning_content`, `reasoning`, or `tool_calls`, or (completions) non-empty `text`. Role-only, empty, usage-only, unparseable, and named non-OpenAI events count 0.
- **Rate** — superseded by § P-20260926-04 (busy-time denominator and tokens-per-chunk calibration). Originally `rates.generation_tps.live = {"tps": units in the last 60 s / 60, "window_s": 60, "estimate": true}` per model; global = sum of the per-model values; `tps` is `0` with no data.
- **Wiring** — `proxy.py` reports units through an optional `Outcome.gen_chunk_observer`, like `first_token_observer`, and never touches the collector. The swapper wires it to `StatsCollector.record_gen_chunks`, which keeps a per-model deque pruned to the live window (60 s originally, 15 s since § P-20260926-04). An observer failure is caught in the proxy so it cannot break a stream.

### Limits
- **OpenAI `data:` streams only** — Anthropic `/v1/messages` and `/v1/responses` named-event streams are not counted, and non-streaming requests never are.
- **One chunk is not always one token** — superseded by § P-20260926-04: chunks are now scaled by a per-model tokens-per-chunk ratio calibrated from finished streams. The ratio can still lag when chunk packing changes.
- **Lag** — superseded by § P-20260926-04: the rate now divides by busy time over a 15 s window and reads `0` when nothing is generating. The page still polls every 5 s.

### Page
- **"Live gen t/s ≈" tile and "Live t/s ≈" column** (after Gen t/s, per model and in the All models row), both with a tooltip (text updated by § P-20260926-04). Values use one decimal; `–` when the value is missing, non-numeric, or zero.
- **Seven tiles without orphans** — the Loaded model tile spans two columns, so the tile grid is 2/4/8 columns (breakpoints 40rem and 72rem) instead of the 6/3/2 grid from P-20260925-01.

### Status
- Implemented under P-20260926-01 with proxy chunk-count tests, collector window tests (fake clock), a mid-stream swapper integration test, and Node page tests.

## P-20260926-02 — Control Panel Matches the Stats Dashboard

### Scope
- **Page only** — only the embedded `CONTROL_HTML` markup, CSS, and render helpers in `src/control.py` changed. The control API, request headers, security rules, element IDs, and the 3 s refresh are unchanged. The 6 original control-panel tests pass unedited; one API test (`tests/test_control_api.py`) now checks the unchanged `<title>` instead of the old heading and still proves `/control` is served locally.

### Look
- **Copied, not shared, CSS** — `/control` copies the `/stats` colour variables (light plus a `prefers-color-scheme: dark` block), fonts, card, tile, dot, and pill styles, and the 80rem width. Sharing one stylesheet is deferred to the `src/stats.py` split (P-20260925-05); until then the two copies can drift, and a test checks that shared colour values match.
- **Header link** — title "Model Switcher" with the subtitle "Control panel · Statistics" linking to `/stats`, mirroring the `/stats` subtitle that links to `/control`.
- **Tiles and cards** — Lifecycle, Active model, Pending model, and In-flight requests are `/stats`-style tiles (2 columns, 4 from 40rem). Models and Runtime settings are cards with headings instead of fieldsets.
- **Status is dot + text** — the Lifecycle tile pairs a dot with a plain-language line (ready "Serving", loading/unloading/shutting down in orange, cleanup failed in red, idle grey); the loaded model's button has a green-bordered style plus a "Loaded" pill; errors show as a red-tinted message with a dot and the error text.
- **Themed controls** — shared button style, accent-coloured "Apply settings", accent checkboxes, and a matching number input. No live indicator (left out by the approved plan).

### Status
- Implemented under P-20260926-02 with static look tests and executable Node tests for the loaded-model marker and lifecycle dot; the 6 original control-panel tests are unedited, and one API check moved from the old heading to the unchanged `<title>`.

## P-20260926-04 — Live Generation Rate: Busy Time and Calibration

Supersedes the **Rate** line of § P-20260926-01. Units, wiring, limits of
chunk detection, and page layout from that section still apply. The window
is 5 s and an engine counter source was added in § P-20261114-01; this
section still defines the estimate path.

### Problem
- **Slow to react** (Revision 2, user request 2026-09-26: "short the window") — the live window is now 15 s (`LIVE_WINDOW_S`), separate from the exact-token windows; `last_60s` / `last_300s` keep 60 s / 300 s.
- **Undercount** — "Live gen t/s ≈" divided chunks by a flat 60 s, so idle seconds counted as zero speed (15 s of generation at 400 t/s showed 100), and one chunk was counted as one token although vLLM/SGLang often pack several tokens per chunk under load. The value was already aggregate across streams.

### Definition
- **Busy time** — per model, the length of the union of decode intervals (first token → stream end) that ended in the last 15 s (any outcome) plus active decodes (`[first token, now]`), clipped to `[now − 15 s, now]`. An ended interval that straddles the window start counts only its part inside the window. Overlapping streams count once.
- **Tokens per chunk** — per model, Σ exact `completion_tokens` / Σ chunks over the last 50 finished successful streaming requests with `completion_tokens ≥ 1` and chunks ≥ 1; `1.0` with no samples. Failed, non-streaming, and usage-less requests add no sample. The proxy keeps a per-stream total in `Outcome.gen_chunks`; the swapper records the sample after a successful stream.
- **Rate** — per model: `tps = chunks in the last 15 s × tokens per chunk / max(busy time, 1 s)`; `0` while the model has no active decode (the page shows `–`). Global: Σ per-model calibrated tokens / max(union busy time across all models, 1 s) — a model finished before a swap and the new model's active decode both count, once each; `0` while no decode is active anywhere.
- **JSON** — `rates.generation_tps.live = {"tps", "window_s": 15, "estimate": true, "busy_s", "tokens_per_chunk"}`; `busy_s` is the unfloored busy time; `tokens_per_chunk` is per model and `null` in the global block. Additive to the P-20260926-01 shape.

### Limits
- **Average, not instant** — the value averages the last 15 s of busy time, so a speed change shows fully after about 15 s.
- **Still an estimate** — calibration uses past requests, so it lags when the chunk packing changes; a stream that is open but silent still counts as busy.
- **Exact metrics unchanged** — `tokens`, `last_60s`/`last_300s`, `avg`, and the aggregates never use chunks or the ratio. Observer and calibration input errors are ignored and cannot break a stream.

### Page
- **Tooltip** — the "Live gen t/s ≈" tile and "Live t/s ≈" column read "Estimated combined speed while generating, last 15 s (streamed chunks × tokens per chunk)"; the tile subtitle reads "estimate · 15 s". No layout change.

### Status
- Implemented under P-20260926-04 with fake-clock collector tests (busy time, union, clipping including an ended interval straddling the window start, union across a model swap, floor, calibration cap, `last_60s` still 60 s), proxy chunk-total tests, and swapper integration tests for calibration and failed/usage-less/non-streaming requests.

## P-20260929-01 — Server-timed prompt speed

### Problem
- **Too low on ExLlamaV3** — user report 2026-09-29: TabbyAPI's own prompt speed was about twice ours. Prompt t/s divided `prompt − cached` by the proxy TTFT, which also holds tokenization and templating, queueing, the first decode step, and the SSE flush; TabbyAPI divides new tokens by GPU prefill time only.
- **Sky-high** — when the cached count was unknown (SGLang without `--enable-cache-report` or when nothing was cached, vLLM without `--enable-prompt-tokens-details`, llama.cpp cache only in `timings.cache_n`), the full prompt was counted as new work over a short TTFT.
- **Early first token** — any event with a non-empty `choices` list marked the first token, so a role-only opening chunk sent before prefill ended could shrink TTFT.

### Sources (pinned)
- **llama.cpp `1c47294`** — `usage.prompt_tokens_details.cached_tokens` always; top-level `timings` (`cache_n`, `prompt_n` = new tokens ≥ 1, `prompt_ms` = prefill only) on the non-stream body and the last stream chunk. No flags.
- **tabbyAPI `816c321` / exllamav3 v1.5.2** — `cached_tokens` always; `usage.prompt_time` (seconds, 10 ms rounding) and `prompt_tokens_per_sec`; llama.cpp-style `timings` with `prompt_n = prompt − cached` and `prompt_ms = prompt_time × 1000`.
- **vLLM `6a6c295`** — `cached_tokens` only with `--enable-prompt-tokens-details`; top-level `metrics.time_to_first_token_ms` (scheduled → first token) on the final usage chunk only with `--enable-per-request-metrics`.
- **SGLang `26d7539`** — `cached_tokens` only with `--enable-cache-report`, and omitted when 0; no prefill duration in the OpenAI response.

### Definition
- **Parsing** — the proxy reads `timings.{prompt_n, prompt_ms, cache_n}`, `usage.prompt_time`, and `metrics.time_to_first_token_ms` from the non-stream body and from every SSE event (last valid value wins, like usage). Only finite non-negative numbers are kept (integers for counts); bools and strings such as `"Indeterminate"` are ignored, and a parsing error never breaks proxying. Logic lives in `src/prefill.py`.
- **Cached count** — `usage.prompt_tokens_details.cached_tokens`, else `timings.cache_n`; this also feeds `tokens.cached`.
- **Sample priority** — per request: (a) `prompt_n ≥ 1` and `prompt_ms > 0` → `prompt_n` tokens over `prompt_ms`; (b) `usage.prompt_time > 0`; (c) `metrics.time_to_first_token_ms > 0`; (d) proxy TTFT (streaming only). (b)–(d) use `prompt − cached` tokens. Fewer than 1 token adds nothing. Only successful requests contribute.
- **Learn per model** — user decision 2026-09-29 (Q1): each model remembers whether it has ever reported a cached count (either source). For (b)–(d), a missing count means 0 once the model has reported; before that the request is left out of Prompt t/s, Prompt total, and Prefill tok and counted in `skipped_unknown_cache`. (a) already excludes the cache. The state lives in the per-model stats and is never reset while running.
- **Non-streaming** — user decision 2026-09-29 (Q2): included when (a)–(c) gives a server time. Aggregate interval: streaming `[max(start, first token − seconds), first token]`; non-streaming `[start, min(start + seconds, end)]`. The aggregate math is otherwise unchanged.
- **First token** — only an event with generated output (non-empty `delta.content` / `reasoning_content` / `reasoning` / `tool_calls`, or completions `text`) marks it; role-only, empty-delta, usage-only, and prefill-progress events do not. This is the same test that counts live-rate chunks.
- **JSON** — additive `rates.prompt_tps.server_timed_share` (server-timed ÷ all prefill tokens, `null` before any) and `rates.prompt_tps.skipped_unknown_cache` (count), per model and global. Other keys keep their meaning.

### Limits
- **Proxy TTFT is still the fallback** — SGLang reports no prefill time, and neither do vLLM without the metrics flag or llama.cpp/TabbyAPI when `prompt_ms` rounds to 0; those requests keep the TTFT-based number, which reads lower than the server's own.
- **Server clocks differ** — vLLM's TTFT starts at scheduling (excludes queueing), TabbyAPI rounds `prompt_time` to 10 ms, so very short prompts can fall through to the next source.
- **Proxy TTFT, TPOT, gen t/s, and the live rate** are unchanged except for the stricter first-token rule.

### Page
- **Tooltip** — the "Prompt t/s" column reads "Prompt processing speed; uses the server's own prefill time when available, else time to first token". No layout change.

### Status
- Implemented under P-20260929-01 with unit tests (`tests/test_prefill_timing.py`) and loopback-fake integration tests through the real proxy and switcher per backend (`tests/test_prefill_backends.py`). README lists the backend flags.

## P-20260929-03 — Server-timed generation speed

### Problem
- **Missing, not undercounted, per request** — user question 2026-09-29 ("exllamav3's tool call is non-streaming ... our token/s may undercount the non-streaming generations"). Decode time was measured only for streams (first output chunk → end), so non-streaming requests had tokens in the counters and `last_60s`/`last_300s` windows but no gen t/s, TPOT, or aggregate sample. A model used only without streaming showed no gen t/s.
- **Combined figures undercounted** — Gen total (`avg_aggregate`), `max_aggregate`, and the live rate dropped non-streaming tokens even though they shared the GPU with overlapping streams.
- **Buffered streams inflated** — a stream whose whole output arrives in one chunk (for example a TabbyAPI tool call) had a decode time of a few milliseconds, inflating gen t/s, Gen total, and `max_aggregate`, giving TPOT ≈ 0, and skewing the live-rate tokens-per-chunk calibration.

### Sources (pinned)
- **llama.cpp `1c47294`** — top-level `timings.predicted_n` / `predicted_ms` (first → last token) on the non-stream body and the last stream chunk. No flags. Its own `predicted_per_second` divides by the same N.
- **tabbyAPI `816c321` / exllamav3 v1.5.2** — `usage.completion_time` (seconds, 10 ms rounding) and llama.cpp-style `timings.predicted_n` / `predicted_ms`. With tools and `stream: true` it is one generation pass: text before the tool tag streams, the tool-call text is held back and emitted in the final (finish) chunk; `usage`/`timings` include the tool-call tokens and time. A reply that opens with a tool call sends nothing until the end.
- **vLLM `6a6c295`** — top-level `metrics.generation_time_ms` (first → last token) on the final usage chunk and the non-stream body, only with `--enable-per-request-metrics` (n = 1).
- **SGLang `26d7539`** — no decode duration in the OpenAI response for streaming; `meta_info` only on non-streaming requests with `return_meta_info`, which the switcher does not set.
- **Tool parsers** — vLLM, SGLang, and llama.cpp tool parsers can also hold back text (a gap, then a burst); the effect is parser-dependent.

### Definition
- **Parsing** — the proxy reads `timings.predicted_n` + `predicted_ms` (accepted only as a valid pair from one event), `usage.completion_time`, and `metrics.generation_time_ms` from the non-stream body and every SSE event; last valid value wins. `timings.prompt_n` + `prompt_ms` are now also taken only as a pair (review note on P-20260929-01), and integers too large for a float are ignored without skipping the event's other fields. Logic lives in `src/decode.py`; parsing errors never break proxying.
- **Sample priority** — per successful request with `completion_tokens ≥ 1`: (a) `predicted_n ≥ 1` and `predicted_ms > 0` → `predicted_n` tokens over `predicted_ms`; (b) `usage.completion_time > 0`; (c) `metrics.generation_time_ms > 0`; (d) the proxy decode time (streaming only), used only when the stream had ≥ 2 generated-output chunks. (b)–(d) use `completion_tokens`. A stream that fails the (d) chunk rule is left out and counted once in `skipped_buffered` (per model and global); a non-streaming request without server time is simply not timed.
- **Convention** — speed is tokens ÷ time (N, not N − 1), matching TabbyAPI and the existing proxy measure; llama.cpp's and vLLM's times span first → last token (N − 1 decode steps), so N ÷ time reads slightly higher than the per-step rate (N − 1) ÷ time; llama.cpp's own `predicted_per_second` also uses N, so it matches ours. TPOT stays `seconds / (tokens − 1)`.
- **Uses** — the sample feeds `rates.generation_tps.avg`, `tpot_ms`, `avg_aggregate`, and `max_aggregate`, streaming or not.
- **Aggregate interval** — server-timed `[max(request start, end − seconds), end]`; proxy-timed `[first chunk, end]` (unchanged). Because a server-timed interval can start anywhere after its request started, a decode group is now published only when no in-flight request of that model (streaming or not, other than the one ending) started before the group ended; before, only active stream decodes held a group. Failed or cancelled requests add no interval, so they still only delay publication and never bridge groups.
- **Live rate** — streams with fewer than 2 output chunks give no tokens-per-chunk calibration sample. Busy time and chunk counting are unchanged; non-streaming requests still do not count.
- **JSON** — additive `rates.generation_tps.server_timed_share` (server-timed ÷ all decode tokens, `null` before any) and `rates.generation_tps.skipped_buffered` (count), per model and global. `last_60s`/`last_300s` are unchanged.

### Limits
- **SGLang** — streaming keeps the proxy timing (with the ≥ 2 chunks rule); non-streaming generation is not timed.
- **vLLM without `--enable-per-request-metrics`** and llama.cpp/TabbyAPI replies whose times round to 0 fall through to the next source, as for prompt speed.
- **Two-chunk bursts** — a held-back tool call streamed in two or more chunks at the end still counts with the proxy time when the server gives no decode time.
- **Proxy TTFT and prompt t/s** are unchanged; a buffered stream's TTFT-based prompt fallback still includes decode time.
- **Held publication** — a long queued, prefilling, or non-streaming request that started before a decode group ended holds that group's "Gen total" (`avg_aggregate`) and `max_aggregate` publication until the request ends (at most the proxy timeout); the published numbers are unchanged.

### Page
- **Tooltip** — the "Gen t/s" column reads "Generation speed; uses the server's own decode time when available, else first to last streamed chunk". No layout change.

### Status
- Implemented under P-20260929-03 with unit tests (`tests/test_decode_timing.py`) and loopback-fake integration tests through the real proxy and switcher per backend (`tests/test_decode_backends.py`). README lists the backend flags. Earlier sections that say non-stream timing is unobservable are kept as history.

## P-20260930-01 — Prompt vs generation time split

### Problem
- **Which side matters** — user request 2026-09-30: show the time used for prompt processing and for token generation, so the user can see which one matters more for a given workload.
- **Raw totals are biased** — per-model `prefill_time` and `decode_time` already existed (for t/s) but cover different request sets: unknown-cache prompts drop out of prefill only (bias toward generation), buffered streams drop out of decode only while their TTFT fallback still holds the decode time (bias toward prompt).

### Definition
- **Paired requests only** — a request counts only when it has both a prefill sample (`select_prefill`, P-20260929-01) and a decode sample (`select_decode`, P-20260929-03); it then adds its own prefill and decode seconds. Only successful requests have samples, so failures never count.
- **Per-request sums** — no overlap merging: two requests prefilling at once add both their times. This is request time, not GPU busy time.
- **Global** — its own accumulator updated with the same request (like the other global token/timing totals), so it equals the sum of the per-model values.
- **JSON** — additive `requests.time_split = {"prefill_s", "decode_s", "prefill_share", "generation_share", "requests"}` per model and global. Shares are raw fractions of `prefill_s + decode_s` (0–1, like `cache_hit_rate`), `null` before any paired request. Other keys and numbers are unchanged.

### Limits
- **Concurrency counts each request** — with overlapping requests the seconds add up to more than wall-clock time; the share is still a fair per-request split.
- **SGLang prefill** — SGLang reports no prefill time, so its prefill side is the switcher's TTFT, which also includes queueing, tokenization, and the first decode step (same for any backend without server prefill time).
- **Fully cached prompts** — a request with no new prompt tokens has no prefill sample and drops out, so heavy cache reuse is under-represented.
- **Swap and load time** — not included; model swaps are tracked separately and only globally.

### Page
- **Column** — new "Prompt share" column right after "Prompt total", showing `prefill_share` as a whole percentage (e.g. `62%`), `–` before any paired request; the cell tooltip gives the seconds ("Prompt 123.4 s · Gen 75.6 s over 42 requests"). The header tooltip explains it is the share of request time spent on prompt processing vs generation, counting only requests where both times are known. The All models row shows the global value.
- **Cache column** — the decorative meter bar is removed (user decision 2026-09-30) to make room; the percentage text stays.

### Status
- Implemented under P-20260930-01 with collector tests (`tests/test_stats_time_split.py`), JSON shape tests, and page tests (`tests/test_stats_page.py`). README describes the column and `time_split`.

## P-20260930-02 — Utilized (busy) time

### Why
- **Uptime alone hides load** — user request 2026-09-30: next to Uptime, show how long the server was actually working.
- **Existing busy times do not fit** — the prefill/decode busy times behind **Gen total** / **Prompt total** are per model, skip ineligible requests, and publish late, so they are not one union of "working" time.

### Definition
- **Any request active** (user choice 2026-09-30) — wall-clock time with at least one request in flight, from arrival to finish, including waiting in the queue and waiting for a model to load. Overlapping requests count once.
- **Counter** — `Swapper._busy_requests`, separate from `_active_requests` ("Active now" is unchanged and still starts after model acquisition). `handle_request` increments it after the 400/404 checks (including a no-model request routed to the running model) and before `_acquire_model`, and releases it in a `try/finally` around acquisition and proxying, so success, upstream errors, rejections (queue full, switch in progress, start failure, timeout), exceptions, cancellation, and client disconnect all release it exactly once.
- **Timer** — `StatsCollector.observe_busy(n)` on `time.monotonic()`: the 0 → 1 edge remembers the start, the 1 → 0 edge adds the period to the total; a snapshot adds the running period.
- **JSON** — additive top-level `utilization = {"busy_seconds": float, "busy_share": float|null}`. `busy_share = busy_seconds ÷ uptime`, where uptime for this ratio uses a monotonic start, clamped to 0–1, `null` when that uptime is 0. Other keys and values are unchanged.

### Limits
- **Includes waiting** — queue wait and model-load wait count as busy, so a slow load with a waiting request shows as busy time.
- **Loads without a request** — a model load with no waiting request (for example a manual load from the control panel) does not count.
- **Rejections before validation** — malformed requests (`400`) and unknown models (`404`) do not count.
- **No-model requests behind a control action** — a request without a model name that waits on the swapper lock behind a control-panel load or unload is not counted during that wait; counting starts only after the 400 check finds a running model.
- **Not GPU time** — it is request time; a busy server may still have idle GPU time (for example while waiting for the client).
- **In memory** — like the other stats, it resets on restart.

### Page
- **Uptime tile sub-line** — the static "since start" sub-line now shows two stacked lines like the Requests tile: "busy <time>" (same duration format as the uptime) and "<N>% of uptime" (whole-number percent; this line is left out when the share is `null` or invalid). Two lines avoid a wrap mid-value at 1280px (review 2026-09-30). It falls back to "since start" when `utilization` is missing or malformed. The tile tooltip explains the definition. No new tile; the tile count and grid are unchanged.

### Status
- Implemented under P-20260930-02 with collector, swapper, and page tests (`tests/test_stats_utilization.py`) and updated key-set/fixture tests. README describes the sub-line and `utilization`.

## P-20260930-03 — Loads replace Swaps on the page

### Why
- **Swaps missed most loads** — user request 2026-09-30: the Swaps tile skipped the first load. `swaps` timed only successful stop-then-start loads, so cold starts, loads after an idle or manual unload, the final load of a superseded chain, and failed loads were not counted, and there were no per-model load times.

### Definition
- **Load** — one process start attempt in `_ensure_model`, whatever triggered it (request, control panel, cold start, after idle unload, swap, crash recovery). The already-healthy early return is not a load, and neither is a failure while stopping the previous model (before any start).
- **Timing** — `load_s` runs from just before the process start until the health check passes (user choice 2026-09-30: the tile average is load only). When a previous model was stopped for this load, that stop (shutdown hook + process stop) is timed separately as `stop_s`, and the load counts as a swap.
- **Outcomes** — a started load that does not become healthy is counted by kind and never timed: `failed` (`model_start_failed`, `swap_timeout`, `process_cleanup_failed`) or `abandoned` (`swap_superseded`, `shutting_down`, `cancelled`). Kind names follow the owner outcome mapping in `Swapper._acquire_model` and `_finish_cancelled_owner`.
- **Recording point** — inside `_ensure_model`: the start → healthy part moved into `_start_until_healthy`, and its single call is wrapped in `try/except BaseException`. Success calls `StatsCollector.record_load`; any exception calls `record_load_outcome` with the mapped kind and is re-raised unchanged. The record is local to that call, so each attempt is counted exactly once and nothing is left pending; a waiter that takes over ownership and runs `_ensure_model` again either starts a new process (a new load) or hits the early return (not a load). For cancellation, the kind is read (not consumed) from the outcome the cancel path already published, with the same fallback `_finish_cancelled_owner` uses. This avoids touching the owner and cancellation code in `src/swapper.py`.
- **JSON** — additive top-level `loads = {"count", "avg_ms", "p95_ms", "max_ms", "swaps", "stop": {"count", "avg_ms", "p95_ms", "max_ms"}, "failed": {kind: n}, "abandoned": {kind: n}}` (duration stats `null` when the count is 0, like `swaps`), and the same shape per model under `models[name].loads`. `swaps` keeps its meaning and numbers; globally `loads.swaps == swaps.count`.

### Limits
- **Crash recovery counts as a swap** — a restart of the same model after its process died stops the old handle first, so it counts as a swap with a stop time, the same as `swaps` always did.
- **Stop time only for successful loads** — like `swaps`, `stop` and the swap count include only loads that became healthy; a stop before a failed or abandoned load is not timed.
- **Kind from the load's own view** — the kind is what ended this load; the response a request finally gets can differ (for example, shutdown during a later cleanup of a different handle).
- **Per-model entry on failure** — a failed load (including a control-panel load that fails to start) creates the model's per-model entry, so the model appears in `models` and as a page row with 0 requests.
- **Unexpected exceptions** — an exception during the start that the owner mapping does not know is counted as `failed` / `model_start_failed`, while the request path still propagates the exception itself (the recording re-raises it unchanged).
- **Stop, then supersede or shutdown before the start** — when a newer request or shutdown is noticed right after the previous model stopped but before the new process starts (`_raise_for_intent`), nothing was started, so it is not a load and that stop is not timed.
- **In memory** — like the other stats, it resets on restart.

### Page
- **Loads tile** — the Swaps tile becomes "Loads" (`kpi-loads`, `kpi-loads-sub`, tile `kpi-loads-tile`): the load count, then two stacked lines like the Requests and Uptime tiles: "avg <s>" and a second line where failures win (user decision 2026-09-30): "<N> failed" (the sum of `failed`) when any load failed, otherwise "<N> swaps" ("1 swap") when swaps > 0; the line is left out when both are 0. Abandoned loads are not shown on the page. Same tile count and grid.
- **Tooltip** — carries the full picture: "Model loads: process start until the health check passes. Swaps: N. Failed: N. Avg stop of the previous model: X s (not included in the average)." Each part after the first appears only when above 0 (the stop part only when a stop was timed).
- **Width** — one short value per line, so both lines fit at 1280px (checked in headless Chrome up to "99,999 swaps" and "99,999 failed").

### Status
- Implemented under P-20260930-03 with swapper-level tests (`tests/test_stats_loads.py`), key-set and contract updates, and page tests (`tests/test_stats_page.py`). README describes the tile and `loads`.

## P-20261114-01 — Live Generation Rate: Engine Counter and 5 s Window

Builds on § P-20260926-04, which still defines busy time and the chunk
estimate. Changes the window and adds a second, exact source.

### Problem
- **Silent streams read near 0** — user report 2026-11-14: "Live gen t/s ≈ 3.4" while vLLM ran at about 200 t/s. On the live server a stream sent its first token, then about 25 s of no counted chunks (a buffered tool call), and ended with 2438 more completion tokens. Busy time kept growing with no chunks, so the tile fell from 125 to 1.9 to 0, and later short replies read 3–50 t/s while that span stayed in the 15 s window (evidence: `artifacts/P-20261114-01/main-02-live-samples.jsonl`).
- **Slow to react** — user decision 2026-11-14: a 5 s window, fixed (no flag).

### Definition
- **Window** — `LIVE_WINDOW_S` is 5 s for both sources. `LIVE_BUSY_FLOOR_S` stays 1 s: engine samples arrive about once per second, so a new decode already spans about 1 s of busy time when its engine rate first shows.
- **Counter table** — `ENGINE_TOKEN_COUNTERS` in `src/engine_metrics.py` lists counters that grow during generation. It holds only `vllm:generation_tokens_total` (checked on the live server 2026-11-14; it grows every engine step). SGLang's `sglang:generation_tokens_total` and llama.cpp's `llamacpp:tokens_predicted_total` grow only when a request finishes (`artifacts/P-20261114-01/web-researcher-01-quotes.txt`), so they would read 0 and then spike; those backends use the estimate. A future live counter is one more table entry.
- **Parsing** — stdlib Prometheus text parsing: comments, blank lines, labels (quoted values may hold `}` or spaces), optional timestamps, and scientific notation (`2.429807e+06`). The value is the sum over all label sets of the first table name present; NaN, infinite, and negative samples are skipped.
- **Polling** — `EngineCounterPoller` (owned by the swapper) starts when a decode records its first token and polls `http://127.0.0.1:{port}/metrics` of the model process directly, not through the client listener. It polls about once per second with a 1 s timeout and its own short-lived HTTP session (not the proxy pool), one poll in flight at most, and stops when the model has no active decode, so an idle switcher sends no requests. The whole body is read up to 4 MB (live vLLM: about 74 KB, counter at line 231). If a first token arrives while a stopping loop is still closing its session, the loop restarts as soon as it ends. Each model process is probed until it answers: a 404, 405, or 501, a body over 4 MB, or a body without a listed counter means estimate only until a new process is loaded. Timeouts, refused connections, and other errors are retried at the normal cadence. Poll errors are logged at debug level and never reach the proxied request.
- **Engine rate** — samples `(time, counter)` older than 5 s are dropped, and all samples are cleared when polling starts again after the model was idle (or on a new process), so time before the idle gap never enters the first reading. Over the span from the oldest to the newest in-window sample: `tps = counter increase / max(busy time in that span, 1 s)`, where busy time is the union of decode intervals from § P-20260926-04. A drop in the counter (engine restart) counts as 0 for that step. With fewer than two in-window samples the model uses the estimate.
- **Zero when idle** — `tps` is `0` while the model has no active decode (first token seen, not ended), with either source, so prompt processing alone reads 0. When one reply streams while another prefills, the counter rises slower and the rate shows that.
- **Global** — the current model's engine rate when it has one and no other model is decoding; otherwise the § P-20260926-04 estimate.
- **JSON** — `live` adds `source`: `"engine"` (then `estimate` is `false`) or `"estimate"`; `window_s` is `5`. `busy_s` stays the busy time in the whole window.

### Limits
- **vLLM only** — other backends keep the estimate and its limits (lag of the tokens-per-chunk ratio, silent streams count as busy).
- **Per engine, not per request** — the counter covers everything the engine generates, including requests that reach it without going through the switcher. That matches the aggregate meaning of the live rate.
- **First second** — a decode after an idle gap shows the estimate until its second sample arrives (about 1 s).
- **Not tested on the live server** — the parser and poller are tested against a fake backend (aiohttp test server); the counter name and its live growth were checked by hand on the live vLLM server.

### Page
- **Tile** — the label is "Live gen t/s" (no "≈") and the subtitle shows the source and window, for example "engine · 5 s" or "estimate · 5 s" (`kpi-live-sub`). The column is "Live t/s". Both tooltips read "Combined speed while generating, last 5 s: the engine's generated-token counter (vLLM), else an estimate from streamed chunks × tokens per chunk".

### Status
- Implemented under P-20261114-01 with parser and poller tests against an aiohttp fake backend (`tests/test_engine_metrics.py`: large streamed bodies, transient errors, the stop/restart race, restart after idle), fake-clock collector tests and swapper integration tests including the 25 s silent-stream regression (`tests/test_stats_live_engine.py`), and the existing live-rate and page tests moved to the 5 s window.

Return to the [decision index](../DECISIONS.md).

"""HTTP proxy handler — routes requests to the active upstream."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from src.prefill import apply_server_prefill

if TYPE_CHECKING:
    from src.stats import Outcome

logger = logging.getLogger(__name__)

# Hop-by-hop headers that must NOT be forwarded (RFC 7230 / RFC 7231).
_HOP_BY_HOP_HEADERS = frozenset([
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
])

# Endpoints where the model name appears in the JSON body.
_MODEL_ENDPOINTS = frozenset([
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/v1/rerank",
    "/v1/reranking",
    "/rerank",
    "/infill",
    "/completion",
    "/v1/images/generations",
    "/v1/images/edits",
    "/v1/audio/speech",
    "/v1/audio/transcriptions",
    "/v1/messages",
    "/v1/responses",
    "/sdapi/v1/txt2img",
    "/sdapi/v1/img2img",
])

# Endpoints where stream_options injection is applied (OpenAI-canonical
# paths served by llama.cpp and vLLM; stream_options is spec-valid on both).
_STREAM_OPTIONS_ENDPOINTS = frozenset([
    "/v1/chat/completions",
    "/v1/completions",
])

# Cap for the pending partial-line SSE buffer in stream_proxy: a
# newline-free stream (e.g. binary audio) is discarded once it exceeds this.
_SSE_LINE_BUFFER_CAP = 64 * 1024


def _int_or_none(value: Any) -> int | None:
    """Return value if it is a plain int, else None."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def inject_stream_options(
    request: web.Request,
    body: bytes | None,
    data: Any,
) -> bytes | None:
    """Return body with stream_options.include_usage added when eligible.

    Guards: POST only; path in _STREAM_OPTIONS_ENDPOINTS; body parses as a
    JSON dict; "stream" is truthy; "stream_options" absent (client-set
    values are never modified). On any re-serialization failure the
    original body is returned.
    """
    if request.method != "POST":
        return body
    if request.path not in _STREAM_OPTIONS_ENDPOINTS:
        return body
    if not body or not isinstance(data, dict):
        return body
    if not data.get("stream"):
        return body
    if "stream_options" in data:
        return body
    try:
        data["stream_options"] = {"include_usage": True}
        return json.dumps(data).encode()
    except (TypeError, ValueError):
        return body


def extract_model(
    request: web.Request,
    body: bytes | None = None,
) -> str | None:
    """Try to extract the model name from a request.

    Checks query string first, then attempts JSON body parse.
    Returns None if model cannot be determined.
    """
    model = request.query.get("model")
    if model:
        return model

    if request.path not in _MODEL_ENDPOINTS:
        return None

    ct = request.content_type or ""
    if "application/json" not in ct:
        return None

    if not body:
        return None

    try:
        data = json.loads(body)
        return data.get("model")
    except (json.JSONDecodeError, TypeError):
        return None


def _openai_error(message: str, code: str = "internal_error") -> dict:
    """Return an OpenAI-compatible error response body."""
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": None,
            "code": code,
        }
    }


def _forward_headers(request: web.Request) -> dict[str, str]:
    """Copy request headers, excluding hop-by-hop headers.

    content-length is dropped: aiohttp recomputes it from the data= bytes,
    which keeps the header consistent when the body is mutated (e.g.
    stream_options injection).
    """
    skip = set(_HOP_BY_HOP_HEADERS)
    skip.add("content-length")
    connection_hdr = request.headers.get("connection", "").lower()
    if connection_hdr:
        # Connection header may list additional hop-by-hop headers
        for extra in connection_hdr.split(","):
            skip.add(extra.strip())

    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() not in skip:
            headers[key] = value
    return headers


async def handle_models_list(
    request: web.Request,
    model_names: list[str],
) -> web.Response:
    """Handle GET /v1/models — return static list of configured models."""
    data = []
    for name in model_names:
        data.append({
            "id": name,
            "object": "model",
            "created": 0,
            "owned_by": "local",
        })
    return web.json_response({"object": "list", "data": data})


def _extract_usage(body: bytes | None, outcome: Outcome) -> None:
    """Populate Outcome token fields from an OpenAI-format usage object.

    Silently ignores unparseable bodies and missing/invalid fields —
    token metrics simply stay None for that request.
    """
    if not body:
        return
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return
    _apply_usage_dict(data, outcome)
    _apply_server_prefill(data, outcome)


def _apply_server_prefill(data: Any, outcome: Outcome) -> None:
    """Copy server prefill timing fields; never breaks proxying."""
    try:
        apply_server_prefill(data, outcome.server_prefill)
    except Exception:
        logger.debug("server prefill parsing failed", exc_info=True)


def _apply_usage_dict(data: Any, outcome: Outcome) -> None:
    """Copy usage fields from a parsed OpenAI response/event dict."""
    if not isinstance(data, dict):
        return
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return
    outcome.prompt_tokens = _int_or_none(usage.get("prompt_tokens"))
    outcome.completion_tokens = _int_or_none(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        outcome.cached_tokens = _int_or_none(details.get("cached_tokens"))


# Delta fields whose non-empty value marks one streamed output unit.
_GEN_DELTA_FIELDS = ("content", "reasoning_content", "reasoning", "tool_calls")


def _count_gen_chunks(event: dict) -> int:
    """Count choices in one event that carry non-empty generated output.

    One unit per choice whose chat delta has content, reasoning, or tool
    calls, or whose completions ``text`` is non-empty. Role-only, empty,
    and usage-only events count 0. This is a chunk count, not a token count.
    """
    choices = event.get("choices")
    if not isinstance(choices, list):
        return 0
    units = 0
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and any(
            delta.get(key) for key in _GEN_DELTA_FIELDS
        ):
            units += 1
        elif choice.get("text"):
            units += 1
    return units


def _process_sse_line(line: bytes, outcome: Outcome) -> None:
    """Parse one complete SSE data line and update the Outcome.

    The first event carrying generated output (content, reasoning, tool
    calls, or completions text) marks the first token; role-only, empty,
    usage-only, and prefill-progress (``prompt_progress``) events do not.
    Any event carrying a usage dict populates the token fields (later
    events overwrite — the final usage chunk wins); server prefill timing
    (``timings``, ``usage.prompt_time``, ``metrics``) is collected the same
    way. Events with generated output are reported to the optional chunk
    observer. Parse failures are silently ignored ([DONE] sentinels,
    comments, partial events).
    """
    text = line.strip()
    if not text.startswith(b"data:"):
        return
    payload = text[len(b"data:"):].strip()
    if not payload or payload == b"[DONE]":
        return
    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(event, dict):
        return
    units = _count_gen_chunks(event)
    # Only generated output marks the first token: a role-only opening
    # chunk or a prefill-progress chunk (llama.cpp / TabbyAPI
    # ``return_progress``) can arrive before prefill has finished.
    if units and outcome.first_token_at is None:
        first_token_at = time.monotonic()
        outcome.first_token_at = first_token_at
        if outcome.first_token_observer is not None:
            outcome.first_token_observer(first_token_at)
    if units:
        # Total per stream calibrates tokens per chunk after the request.
        outcome.gen_chunks += units
        if outcome.gen_chunk_observer is not None:
            try:
                outcome.gen_chunk_observer(units)
            except Exception:
                # The live estimate must never break the proxied stream.
                logger.debug("gen_chunk_observer failed", exc_info=True)
    _apply_usage_dict(event, outcome)
    _apply_server_prefill(event, outcome)


async def proxy_request(
    request: web.Request,
    upstream_url: str,
    body: bytes | None,
    *,
    timeout: float = 60,
    client: aiohttp.ClientSession | None = None,
    outcome: Outcome | None = None,
) -> web.Response:
    """Forward a request to the upstream server.

    Args:
        request: The incoming aiohttp request.
        upstream_url: Base URL of the upstream.
        body: Pre-read request body bytes.
        timeout: Maximum time to wait for the response.
        client: Optional shared client session.
        outcome: Optional Outcome object; receives the response byte count.
    """
    upstream_path = request.path_qs or request.path
    target = upstream_url + upstream_path
    headers = _forward_headers(request)

    own_client = client is None
    if client is None:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        client = aiohttp.ClientSession(timeout=client_timeout)

    try:
        async with client.request(
            method=request.method,
            url=target,
            headers=headers,
            data=body,
            ssl=False,
        ) as upstream_resp:
            response_body = await upstream_resp.read()
            if outcome is not None:
                outcome.bytes_out = len(response_body)
                _extract_usage(response_body, outcome)
            # Forward all upstream headers except hop-by-hop
            resp_headers = {}
            for k, v in upstream_resp.headers.items():
                if k.lower() not in _HOP_BY_HOP_HEADERS:
                    resp_headers[k] = v
            return web.Response(
                status=upstream_resp.status,
                body=response_body,
                headers=resp_headers,
            )
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        logger.error("Upstream error: %s", exc)
        return web.json_response(
            _openai_error(f"Upstream error: {exc}"),
            status=502,
        )
    finally:
        if own_client:
            await client.close()


async def stream_proxy(
    request: web.Request,
    upstream_url: str,
    body: bytes | None,
    *,
    timeout: float = 60,
    client: aiohttp.ClientSession | None = None,
    outcome: Outcome | None = None,
) -> web.Response:
    """Proxy with chunked streaming for SSE responses."""
    upstream_path = request.path_qs or request.path
    target = upstream_url + upstream_path
    headers = _forward_headers(request)

    own_client = client is None
    if client is None:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        client = aiohttp.ClientSession(timeout=client_timeout)

    if timeout > 0:
        stream_timeout = aiohttp.ClientTimeout(
            total=None,
            connect=timeout,
            sock_read=timeout,
        )
    else:
        stream_timeout = client.timeout

    try:
        async with client.request(
            method=request.method,
            url=target,
            headers=headers,
            data=body,
            ssl=False,
            timeout=stream_timeout,
        ) as upstream_resp:

            # Merge upstream headers with proxy-required headers
            resp_headers = {}
            for k, v in upstream_resp.headers.items():
                if k.lower() not in _HOP_BY_HOP_HEADERS:
                    resp_headers[k] = v
            # Override with proxy-required headers
            resp_headers.setdefault(
                "Content-Type",
                upstream_resp.content_type or "text/event-stream",
            )
            resp_headers["Cache-Control"] = "no-cache"
            resp_headers["X-Accel-Buffering"] = "no"

            response = web.StreamResponse(
                status=upstream_resp.status,
                headers=resp_headers,
            )
            await response.prepare(request)

            try:
                buffer = b""
                async for chunk in upstream_resp.content.iter_any():
                    await response.write(chunk)
                    if outcome is not None:
                        outcome.bytes_out += len(chunk)
                        # Line-buffered SSE parsing: chunks can split lines,
                        # so process complete lines and keep the remainder.
                        buffer += chunk
                        *lines, buffer = buffer.split(b"\n")
                        if len(buffer) > _SSE_LINE_BUFFER_CAP:
                            # Pathological newline-free stream (e.g. binary
                            # audio) — not line-shaped SSE; discard to bound
                            # memory. Byte counts continue via outcome.bytes_out.
                            buffer = b""
                        for line in lines:
                            _process_sse_line(line, outcome)
                if outcome is not None and buffer.strip():
                    # Final line without trailing newline (defensive).
                    _process_sse_line(buffer, outcome)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                logger.error("Stream proxy error: %s", exc)
                if outcome is not None:
                    outcome.stream_error = True
                error_payload = json.dumps(
                    _openai_error(f"Upstream error: {exc}")
                )
                await response.write(
                    f"data: {error_payload}\n\n".encode()
                )
            finally:
                await response.write_eof()

            return response
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        # Error before response.prepare() — safe to return json_response
        logger.error("Stream proxy error (pre-prepare): %s", exc)
        return web.json_response(
            _openai_error(f"Upstream error: {exc}"),
            status=502,
        )
    finally:
        if own_client:
            await client.close()

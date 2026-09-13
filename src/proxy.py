"""HTTP proxy handler — routes requests to the active upstream."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

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
    """Copy request headers, excluding hop-by-hop headers."""
    skip = set(_HOP_BY_HOP_HEADERS)
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
                async for chunk in upstream_resp.content.iter_any():
                    await response.write(chunk)
                    if outcome is not None:
                        outcome.bytes_out += len(chunk)
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

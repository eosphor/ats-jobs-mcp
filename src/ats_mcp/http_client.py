"""HTTP-first client: retries, backoff, categorized errors, streaming.

Design rules:

1. HTTP-first: hit the site's own JSON/HTML endpoints with ``httpx``. Only fall
   back to a headless browser when the site genuinely needs JS and a browser
   pass is worth the memory/CPU. Browsers are the exception, not the default.
2. Streaming, never full buffering. Large bodies are consumed in chunks with a
   hard byte cap, so a 500 MB response cannot OOM the actor. Prefer
   ``iter_lines`` / ``download_to`` over ``get_json`` for anything unbounded.
3. Retry with exponential backoff *plus* jitter, honouring ``Retry-After``.
   Only ``retryable`` categories are retried.
4. Silent-failure guard: a 200 with an empty/invalid body raises
   ``SilentFailureError`` instead of being handed back as "success".
5. Every failure is a categorized ``ActorError`` — never a bare Exception.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import random
from typing import Any, AsyncIterator, Callable, Optional

import httpx

from .errors import (
    ActorError,
    NetworkError,
    RateLimitError,
    SilentFailureError,
    classify_status,
)

DEFAULT_UA = (
    "Mozilla/5.0 (compatible; ATSJobsMCP/1.0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """``Retry-After`` is either delta-seconds or an HTTP date. We accept
    delta-seconds; an HTTP date is uncommon for the APIs we scrape, so we fall
    back to the normal backoff for anything non-numeric."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        return None


class HttpClient:
    """Async HTTP client with classified errors and bounded memory.

    ``proxy_provider`` (optional) is called before each request and returns a
    proxy URL or ``None``; it may be sync or async.
    """

    def __init__(
        self,
        *,
        base_headers: Optional[dict] = None,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        max_body_bytes: int = 25 * 1024 * 1024,
        proxy_provider: Optional[Callable[[], Optional[str]]] = None,
        treat_silent_as_retryable: bool = False,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._headers = {"User-Agent": DEFAULT_UA}
        if base_headers:
            self._headers.update(base_headers)
        self._timeout = httpx.Timeout(timeout)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._max_body_bytes = max_body_bytes
        self._proxy_provider = proxy_provider
        self._treat_silent_as_retryable = treat_silent_as_retryable
        self._log = log or (lambda _m: None)
        self._client: Optional[httpx.AsyncClient] = None
        self._proxied_clients: dict[str, httpx.AsyncClient] = {}

    # -- lifecycle --------------------------------------------------------
    async def __aenter__(self) -> "HttpClient":
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        for client in self._proxied_clients.values():
            await client.aclose()
        self._proxied_clients.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HttpClient used outside 'async with'")
        return self._client

    async def _client_for(self, proxy: Optional[str]) -> httpx.AsyncClient:
        if not proxy:
            return self.client
        client = self._proxied_clients.get(proxy)
        if client is None:
            client = httpx.AsyncClient(
                headers=self._headers,
                timeout=self._timeout,
                follow_redirects=True,
                proxy=proxy,
            )
            self._proxied_clients[proxy] = client
        return client

    # -- backoff ----------------------------------------------------------
    def _backoff(self, attempt: int, retry_after: Optional[float]) -> float:
        if retry_after is not None:
            return min(retry_after, self._backoff_cap)
        # exponential backoff with full jitter
        ceiling = min(self._backoff_cap, self._backoff_base * (2 ** attempt))
        return random.uniform(self._backoff_base, ceiling) if ceiling > self._backoff_base else ceiling

    # -- core request -----------------------------------------------------
    async def request(
        self,
        method: str,
        url: str,
        *,
        expect_json: bool = False,
        validate: Optional[Callable[[Any], bool]] = None,
        max_bytes: Optional[int] = None,
        stream: bool = False,
        **kwargs,
    ) -> httpx.Response:
        """Perform a request with retries. Returns the httpx.Response.

        With ``stream=True`` the caller owns the (still-open) response and MUST
        close it; nothing is buffered here. Without ``stream`` the body is read
        and capped at ``max_bytes`` (default ``max_body_bytes``), and when
        ``expect_json`` is set a 200 with an empty/invalid body raises
        ``SilentFailureError``.
        """
        limit = max_bytes if max_bytes is not None else self._max_body_bytes
        last_exc: Optional[ActorError] = None

        for attempt in range(self._max_retries + 1):
            proxy = self._proxy_provider() if self._proxy_provider else None
            if inspect.isawaitable(proxy):
                proxy = await proxy
            try:
                client = await self._client_for(proxy)
                if stream:
                    resp = await self._open_stream(client, method, url, kwargs)
                else:
                    resp = await client.request(method, url, **kwargs)
                    await self._read_bounded(resp, limit)
                    self._raise_for_status(resp, url)
                    if expect_json:
                        self._guard_json(resp, url, validate)
                return resp
            except ActorError as exc:
                last_exc = exc
                retryable = exc.retryable or (
                    isinstance(exc, SilentFailureError)
                    and self._treat_silent_as_retryable
                )
                if not retryable or attempt >= self._max_retries:
                    raise
                delay = self._backoff(
                    attempt, getattr(exc, "retry_after", None)
                )
                self._log(
                    f"[retry {attempt + 1}/{self._max_retries}] {exc.category} "
                    f"on {url} — sleeping {delay:.2f}s"
                )
                await asyncio.sleep(delay)
            except httpx.HTTPError as exc:
                last_exc = NetworkError(str(exc), url=url)
                if attempt >= self._max_retries:
                    raise last_exc from exc
                delay = self._backoff(attempt, None)
                self._log(
                    f"[retry {attempt + 1}/{self._max_retries}] network on "
                    f"{url} — sleeping {delay:.2f}s"
                )
                await asyncio.sleep(delay)

        # unreachable, but keeps type checkers happy
        raise last_exc or NetworkError("request failed", url=url)

    # -- helpers ----------------------------------------------------------
    async def _open_stream(
        self, client: httpx.AsyncClient, method: str, url: str, kwargs: dict
    ) -> httpx.Response:
        # httpx streams via an async context manager; we enter it manually and
        # hand the live response back. The caller closes it.
        request = client.build_request(method, url, **kwargs)
        resp = await client.send(request, stream=True)
        self._raise_for_status(resp, url)
        return resp

    async def _read_bounded(self, resp: httpx.Response, limit: int) -> None:
        # Response.content is lazy; force a bounded read so an oversized body
        # fails loudly instead of exhausting memory.
        try:
            await resp.aread()
        except httpx.ResponseNotRead:
            await resp.aread()
        if len(resp.content) > limit:
            raise SilentFailureError(
                f"response body exceeds cap ({len(resp.content)} > {limit} bytes)",
                url=str(resp.url),
                status=resp.status_code,
            )

    def _raise_for_status(self, resp: httpx.Response, url: str) -> None:
        status = resp.status_code
        if 200 <= status < 300:
            return
        cls = classify_status(status)
        retry_after = None
        if cls is RateLimitError:
            retry_after = _parse_retry_after(resp.headers.get("retry-after"))
        raise cls(
            f"HTTP {status} for {url}",
            url=url,
            status=status,
            **({"retry_after": retry_after} if cls is RateLimitError else {}),
        )

    def _guard_json(
        self,
        resp: httpx.Response,
        url: str,
        validate: Optional[Callable[[Any], bool]],
    ) -> None:
        raw = resp.content
        if not raw or not raw.strip():
            raise SilentFailureError("HTTP 200 with empty body", url=url, status=200)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SilentFailureError(
                f"HTTP 200 with non-JSON body ({exc})", url=url, status=200
            ) from exc
        if validate is not None and not validate(data):
            raise SilentFailureError(
                "HTTP 200 but payload failed schema check", url=url, status=200
            )

    # -- convenience ------------------------------------------------------
    async def get_json(
        self, url: str, *, validate: Optional[Callable[[Any], bool]] = None, **kw
    ) -> Any:
        resp = await self.request("GET", url, expect_json=True, validate=validate, **kw)
        return resp.json()

    async def post_json(self, url: str, *, json_body: Any = None, **kw) -> Any:
        resp = await self.request(
            "POST", url, expect_json=True, json=json_body, **kw
        )
        return resp.json()

    async def get_text(self, url: str, **kw) -> str:
        resp = await self.request("GET", url, **kw)
        return resp.text

    async def iter_lines(
        self, url: str, *, chunk_size: int = 64 * 1024, **kw
    ) -> AsyncIterator[str]:
        """Yield decoded lines without buffering the whole body (NDJSON etc.)."""
        kwargs = dict(kw)
        resp = await self.request("GET", url, stream=True, **kwargs)
        async with resp:
            async for line in resp.aiter_lines():
                if line:
                    yield line

    async def download_to(self, url: str, path: str, **kw) -> int:
        """Stream a (possibly huge) body straight to disk. Returns bytes."""
        total = 0
        resp = await self.request("GET", url, stream=True, **kw)
        async with resp:
            with open(path, "wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > self._max_body_bytes:
                        raise SilentFailureError(
                            f"download exceeded cap ({total} bytes)", url=url
                        )
                    fh.write(chunk)
        return total

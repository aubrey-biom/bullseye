"""Async Kiteworks REST client built on httpx.

Single shared `httpx.AsyncClient` per process, with:
  * Bearer auth from AuthManager (refreshed lazily on each call).
  * `X-Kiteworks-Version` header from settings.
  * One 401-triggered retry that forces a token refresh, then replays.
  * tenacity retry for 5xx and transient connection errors (exp backoff).
  * `Retry-After` honored on 429.
  * Host pinning: only `settings.kiteworks_base_url` hostname is permitted.
"""

from __future__ import annotations

import shutil
import urllib.parse
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random_exponential,
)

from .auth import AuthManager
from .config import Settings
from .logging_setup import get_logger

logger = get_logger(__name__)

_DEFAULT_PAGE_LIMIT = 200


class KiteworksAPIError(RuntimeError):
    """Non-retryable Kiteworks API failure. Carries status + body."""

    def __init__(self, status: int, message: str, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return isinstance(exc, httpx.TransportError | httpx.RemoteProtocolError | httpx.ReadError)


def make_http_client(settings: Settings) -> httpx.AsyncClient:
    """Build the shared httpx.AsyncClient with a host-pin event hook."""
    expected_host = urllib.parse.urlparse(settings.base_url).hostname

    async def _host_guard(request: httpx.Request) -> None:
        if request.url.host != expected_host:
            raise KiteworksAPIError(
                0,
                f"Outbound call to {request.url.host!r} blocked. "
                f"Only {expected_host!r} is allowed (§15).",
            )

    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.bpd_http_timeout, connect=10.0),
        http2=True,
        follow_redirects=True,
        event_hooks={"request": [_host_guard]},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


class KiteworksClient:
    """Thin async wrapper exposing only the endpoints we need."""

    def __init__(self, settings: Settings, auth: AuthManager, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._auth = auth
        self._http = http

    # ---------- core request helpers ----------

    def _common_headers(self, token: str, *, accept_json: bool = True) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Kiteworks-Version": self._settings.kiteworks_api_version,
        }
        if accept_json:
            headers["Accept"] = "application/json"
        return headers

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._settings.base_url}{path}"

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=0.5, max=30),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                token = await self._auth.get_access_token()
                resp = await self._http.request(
                    method, url, params=params, headers=self._common_headers(token)
                )

                if resp.status_code == 401:
                    # Force-refresh once, replay once.
                    logger.info("api_401_refresh", path=path)
                    token = await self._auth.force_refresh()
                    resp = await self._http.request(
                        method,
                        url,
                        params=params,
                        headers=self._common_headers(token),
                    )

                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        # Convert to retryable by raising a transport error.
                        import asyncio

                        await asyncio.sleep(min(int(retry_after), 60))
                    raise httpx.HTTPStatusError(
                        "rate limited", request=resp.request, response=resp
                    )

                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"server error {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )

                if resp.status_code >= 400:
                    raise KiteworksAPIError(
                        resp.status_code,
                        f"Kiteworks {method} {path} -> HTTP {resp.status_code}",
                        body=resp.text,
                    )

                try:
                    return resp.json()
                except ValueError:
                    return resp.text
        raise RuntimeError("unreachable")  # for type checker

    # ---------- public endpoints ----------

    async def whoami(self) -> dict[str, Any]:
        """GET /rest/users/me — returns the current user profile."""
        return await self._request_json("GET", "/rest/users/me")

    async def list_top_folders(
        self,
        *,
        limit: int = _DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        mode: str = "full_no_links",
    ) -> list[dict[str, Any]]:
        """GET /rest/folders/top — paginate via metadata.total."""
        out: list[dict[str, Any]] = []
        while True:
            payload = await self._request_json(
                "GET",
                "/rest/folders/top",
                params={"limit": limit, "offset": offset, "mode": mode},
            )
            # Per spec: {data: [...], metadata: {total, limit, offset}}
            if isinstance(payload, dict):
                page = payload.get("data") or []
                total = (payload.get("metadata") or {}).get("total")
            elif isinstance(payload, list):
                page = payload
                total = None
            else:
                page = []
                total = None
            out.extend(page)
            if not page or len(page) < limit:
                break
            offset += limit
            if total is not None and offset >= total:
                break
        return out

    async def list_folder_children(
        self,
        folder_id: str,
        *,
        limit: int = _DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        name_filter: str | None = None,
        extensions: str | None = None,
        deleted: bool | None = False,
        mode: str = "full_no_links",
    ) -> list[dict[str, Any]]:
        """GET /rest/folders/{id}/children — paginate; returns raw array.

        OpenAPI spec confirms the response is an unwrapped array; we walk until
        the page is shorter than `limit`.
        """
        out: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {"limit": limit, "offset": offset, "mode": mode}
            if name_filter:
                params["name"] = name_filter
            if extensions:
                params["extensions"] = extensions
            if deleted is not None:
                params["deleted"] = str(deleted).lower()
            payload = await self._request_json(
                "GET",
                f"/rest/folders/{folder_id}/children",
                params=params,
            )
            if isinstance(payload, dict):
                page = payload.get("data") or []
            elif isinstance(payload, list):
                page = payload
            else:
                page = []
            out.extend(page)
            if not page or len(page) < limit:
                break
            offset += limit
        return out

    async def get_folder(
        self, folder_id: str, *, mode: str = "full_no_links", with_: str | None = None
    ) -> dict[str, Any]:
        """GET /rest/folders/{id}."""
        params: dict[str, Any] = {"mode": mode}
        if with_:
            params["with"] = with_
        result = await self._request_json("GET", f"/rest/folders/{folder_id}", params=params)
        return result if isinstance(result, dict) else {}

    async def get_file_metadata(
        self, file_id: str, *, mode: str = "full_no_links"
    ) -> dict[str, Any]:
        """GET /rest/files/{id}."""
        result = await self._request_json(
            "GET", f"/rest/files/{file_id}", params={"mode": mode}
        )
        return result if isinstance(result, dict) else {}

    async def search(
        self,
        query: str,
        *,
        object_id: str | None = None,
        search_type: str = "f",
        include_content: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /rest/query — ad-hoc file/folder/email search."""
        params: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "searchType": search_type,
            "includeContent": str(include_content).lower(),
        }
        if object_id:
            params["objectId"] = object_id
        result = await self._request_json("GET", "/rest/query", params=params)
        return result if isinstance(result, dict) else {"data": result}

    async def stream_file(self, file_id: str) -> AsyncIterator[bytes]:
        """Yield bytes from GET /rest/files/{id}/content. Single attempt + 401 replay."""
        url = f"{self._settings.base_url}/rest/files/{file_id}/content"
        token = await self._auth.get_access_token()
        headers = self._common_headers(token, accept_json=False)
        async with self._http.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 401:
                token = await self._auth.force_refresh()
                headers = self._common_headers(token, accept_json=False)
                # Re-open the stream.
                resp_closed = True
            else:
                resp_closed = False

            if resp_closed:
                async with self._http.stream("GET", url, headers=headers) as resp2:
                    if resp2.status_code >= 400:
                        body = await resp2.aread()
                        raise KiteworksAPIError(
                            resp2.status_code,
                            f"download file {file_id} -> HTTP {resp2.status_code}",
                            body=body.decode("utf-8", errors="replace"),
                        )
                    async for chunk in resp2.aiter_bytes(chunk_size=1024 * 1024):
                        yield chunk
                return

            if resp.status_code >= 400:
                body = await resp.aread()
                raise KiteworksAPIError(
                    resp.status_code,
                    f"download file {file_id} -> HTTP {resp.status_code}",
                    body=body.decode("utf-8", errors="replace"),
                )
            async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                yield chunk

    async def download_file(self, file_id: str, dest_path: Path) -> int:
        """Stream a file to disk. Returns bytes written. Retries with backoff on transport errors."""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest_path.with_suffix(dest_path.suffix + ".part")

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_random_exponential(multiplier=1, max=30),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                tmp.unlink(missing_ok=True)
                bytes_written = 0
                with tmp.open("wb") as f:
                    async for chunk in self.stream_file(file_id):
                        f.write(chunk)
                        bytes_written += len(chunk)
                shutil.move(str(tmp), str(dest_path))
                logger.info(
                    "file_downloaded",
                    file_id=file_id,
                    path=str(dest_path),
                    bytes=bytes_written,
                )
                return bytes_written
        raise RuntimeError("unreachable")

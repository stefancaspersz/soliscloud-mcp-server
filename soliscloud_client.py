"""
Minimal async client for the SolisCloud Monitoring API.

SolisCloud uses a custom HMAC-SHA1 request-signing scheme (similar to the one
used by Aliyun OSS):

    StringToSign = VERB + "\n"
                 + Content-MD5 + "\n"
                 + Content-Type + "\n"
                 + Date + "\n"
                 + CanonicalizedResource

    Signature = Base64(HMAC-SHA1(StringToSign, KeySecret))
    Authorization = "API " + KeyId + ":" + Signature

All calls are HTTP POST with a JSON body (an empty JSON object `{}` if the
endpoint takes no parameters). The Date header must be within ~15 minutes of
the SolisCloud server's clock or requests are rejected.

Built against the SolisCloud Platform API Document V2.0.3 (revised 24 Dec 2025),
downloadable from:
https://usservice.solisinverters.com/support/solutions/articles/73000591225-soliscloud-api-guide

See also https://github.com/hultenvp/soliscloud_api for a well-tested reference
implementation of the same signing scheme.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.request
from email.utils import formatdate
from typing import Any

DEFAULT_BASE_URL = "https://www.soliscloud.com:13333"


class SolisCloudAPIError(RuntimeError):
    """Raised when the SolisCloud API returns a non-success response."""

    def __init__(self, message: str, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class SolisCloudClient:
    """Thin async wrapper around the SolisCloud Monitoring API."""

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
    ) -> None:
        if not key_id or not key_secret:
            raise ValueError("SolisCloud key_id and key_secret are required")
        self._key_id = key_id
        self._key_secret = key_secret.encode("utf-8")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> "SolisCloudClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _sign(self, path: str, body_bytes: bytes) -> dict[str, str]:
        content_md5 = base64.b64encode(hashlib.md5(body_bytes).digest()).decode("utf-8")
        content_type = "application/json"
        date_str = formatdate(timeval=None, localtime=False, usegmt=True)

        string_to_sign = "\n".join(["POST", content_md5, content_type, date_str, path])
        signature = base64.b64encode(
            hmac.new(self._key_secret, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")

        return {
            "Content-MD5": content_md5,
            "Content-Type": content_type,
            "Date": date_str,
            "Authorization": f"API {self._key_id}:{signature}",
        }

    def _post(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    async def call(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Call a SolisCloud endpoint, e.g. path='/v1/api/userStationList'."""
        if not path.startswith("/"):
            path = "/" + path
        body = json.dumps(params or {}, separators=(",", ":")).encode("utf-8")
        headers = self._sign(path, body)

        url = self._base_url + path
        status_code, content = await asyncio.to_thread(self._post, url, body, headers)

        try:
            data = json.loads(content)
        except ValueError as exc:
            raise SolisCloudAPIError(
                f"Non-JSON response ({status_code}): {content[:500].decode('utf-8', errors='replace')}",
                status_code=status_code,
            ) from exc

        if status_code != 200:
            raise SolisCloudAPIError(
                data.get("msg", f"HTTP {status_code}"),
                code=str(data.get("code")) if isinstance(data, dict) else None,
                status_code=status_code,
            )

        # SolisCloud wraps results as {"success": true/false, "code": "0", "msg": ..., "data": ...}
        if isinstance(data, dict) and data.get("success") is False:
            raise SolisCloudAPIError(
                data.get("msg", "SolisCloud API call failed"),
                code=str(data.get("code")),
            )

        return data.get("data") if isinstance(data, dict) else data

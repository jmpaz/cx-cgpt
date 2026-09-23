from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from ..http import NoRedirect, TransportError
from .session import ORG_ENV, SESSION_ENV, Session, load_session

ORIGIN = "https://claude.ai"
MAX_BYTES = 64 * 1024 * 1024
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_ALLOWED = re.compile(
    rf"/api/organizations/{_UUID}/(?:chat_conversations/{_UUID}|chat_conversations_v2|conversation/search/v2"
    rf"|conversations/{_UUID}/wiggle/(?:list-files|download-file))"
)
SNIPPET_CHARS = 200


class ClaudeClient:
    def __init__(self, *, session: Callable[[], Session] = load_session,
                 timeout: float = 30, opener: Any = None):
        self._load_session = session
        self._session: Session | None = None
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self._session = None

    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = self._load_session()
        return self._session

    @property
    def organization(self) -> str:
        organization = self.session.organization
        if organization is None:
            raise TransportError(f"No claude.ai organization is known; set {ORG_ENV}.")
        return organization

    def conversation(self, conversation_id: str) -> dict:
        return self.get(
            f"/api/organizations/{self.organization}/chat_conversations/{conversation_id}",
            {"tree": "True", "rendering_mode": "messages", "render_all_tools": "true"},
        )

    def conversations(self, *, limit: int, offset: int) -> dict:
        return self.get(
            f"/api/organizations/{self.organization}/chat_conversations_v2",
            {"limit": limit, "offset": offset, "consistency": "eventual"},
        )

    def search(self, query: str, *, limit: int, project: str | None = None) -> dict:
        params = {"query": query, "n": limit, "target_snippet_size": SNIPPET_CHARS}
        if project:
            params["project_uuid"] = project
        return self.get(f"/api/organizations/{self.organization}/conversation/search/v2", params)

    def output_files(self, conversation_id: str) -> dict:
        return self.get(f"/api/organizations/{self.organization}/conversations/{conversation_id}/wiggle/list-files")

    def output_file(self, conversation_id: str, path: str, *, limit: int) -> bytes:
        return self.fetch(
            f"/api/organizations/{self.organization}/conversations/{conversation_id}/wiggle/download-file",
            {"path": path}, accept="*/*", limit=limit,
        )

    def get(self, path: str, params: dict | None = None) -> dict:
        content = self.fetch(path, params)
        try:
            result = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            raise TransportError("claude.ai returned invalid JSON.") from None
        if not isinstance(result, dict):
            raise TransportError("claude.ai returned an unexpected response shape.")
        return result

    def fetch(self, path: str, params: dict | None = None, *,
              accept: str = "application/json", limit: int = MAX_BYTES) -> bytes:
        if not _ALLOWED.fullmatch(path):
            raise TransportError("Unsupported claude.ai endpoint.")
        query = urllib.parse.urlencode(params or {})
        request = urllib.request.Request(
            ORIGIN + path + ("?" + query if query else ""),
            headers={"Cookie": "sessionKey=" + self.session.key, "Accept": accept,
                     "User-Agent": "cx-chats/0.1.0"},
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            kind = error.headers.get("Content-Type", "") if error.headers else ""
            try:
                body = error.read(65536)
            except OSError:
                body = b""
            error.close()
            raise TransportError(_failure(status, kind, body, self.session.source)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TransportError("claude.ai request failed; check network connectivity.") from None
        if len(content) > limit:
            raise TransportError(f"claude.ai response exceeded the size limit of {limit:,} bytes.")
        return content


def _failure(status: int, content_type: str, body: bytes, source: str) -> str:
    if "json" not in content_type:
        return f"claude.ai request failed (HTTP {status}) with a non-JSON page, likely a browser challenge."
    try:
        error = json.loads(body).get("error") or {}
        details = error.get("details") or {}
        reason = " ".join(str(part) for part in (error.get("message"), details.get("error_code")) if part)
    except (ValueError, UnicodeDecodeError, AttributeError):
        reason = ""
    message = f"claude.ai request failed (HTTP {status}" + (f": {reason})." if reason else ").")
    if status in (401, 403):
        origin = "Chrome" if source.startswith("chrome:") else SESSION_ENV
        message += f" If the session from {origin} has expired, sign in to claude.ai again."
    elif status == 404:
        message += f" If the conversation belongs to another organization, set {ORG_ENV}."
    return message

from __future__ import annotations

import base64
import json
import os
import re
import selectors
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..http import NoRedirect, TransportError
from ..transcript import label

BACKEND_ORIGIN = "https://chatgpt.com"
BACKEND = BACKEND_ORIGIN + "/backend-api"
DOWNLOAD_PATH = "/backend-api/estuary/content"
MAX_BYTES = 64 * 1024 * 1024
_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_ENDPOINT = re.compile(
    rf"/conversations(?:/search)?|/conversation/{_UUID}(?:/interpreter/download)?|/files/download/file[-_][0-9A-Za-z]+"
)


class CodexAuth:
    def __init__(self, executable: str | None = None, timeout: float = 30):
        self.executable = executable or os.environ.get("CX_CHATGPT_CODEX", "codex")
        self.timeout = timeout
        self._process: subprocess.Popen | None = None
        self._buffer = b""
        self._next_id = 0

    def _start(self) -> None:
        if self._process is not None:
            return
        try:
            self._process = subprocess.Popen(
                [self.executable, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            self._request("initialize", {"clientInfo": {"name": "cx-chats", "version": "0.1.0"}})
            self._send({"method": "initialized"})
        except OSError:
            self.close()
            raise TransportError("Cannot start Codex app-server; install Codex or set CX_CHATGPT_CODEX.") from None
        except Exception:
            self.close()
            raise

    def _send(self, value: dict) -> None:
        assert self._process is not None and self._process.stdin is not None
        try:
            self._process.stdin.write(json.dumps(value).encode() + b"\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise TransportError("Codex app-server closed its input unexpectedly.") from None

    def _request(self, method: str, params: dict) -> dict:
        assert self._process is not None and self._process.stdout is not None
        self._next_id += 1
        request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self._process.stdout, selectors.EVENT_READ)
            while True:
                if time.monotonic() >= deadline:
                    raise TransportError("Codex authentication request timed out.")
                if b"\n" not in self._buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TransportError("Codex authentication request timed out.")
                    chunk = os.read(self._process.stdout.fileno(), 65536)
                    if not chunk:
                        raise TransportError("Codex app-server exited before responding.")
                    self._buffer += chunk
                    if len(self._buffer) > 1048576:
                        raise TransportError("Codex app-server response exceeded the size limit.")
                    continue
                line, self._buffer = self._buffer.split(b"\n", 1)
                try:
                    response = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    raise TransportError("Codex app-server returned invalid JSON.") from None
                if not isinstance(response, dict) or response.get("id") != request_id:
                    continue
                if "error" in response:
                    raise TransportError(f"Codex app-server rejected {method}; check Codex login and version.")
                result = response.get("result")
                if not isinstance(result, dict):
                    raise TransportError("Codex app-server returned an invalid response.")
                return result

    def token(self, refresh: bool = False) -> str:
        self._start()
        result = self._request("getAuthStatus", {"includeToken": True, "refreshToken": refresh})
        token = result.get("authToken")
        if result.get("authMethod") != "chatgpt" or not isinstance(token, str) or not token:
            raise TransportError("Sign in to ChatGPT with Codex on this machine before reading ChatGPT history.")
        return token

    def close(self) -> None:
        process, self._process = self._process, None
        self._buffer = b""
        if process is None:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if process.stdout:
            process.stdout.close()


def _account_id(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        auth = claims.get("https://api.openai.com/auth", {})
        value = auth.get("chatgpt_account_id") or auth.get("account_id")
        return value if isinstance(value, str) else None
    except (IndexError, ValueError, TypeError, AttributeError):
        return None


def _provided(info: dict) -> dict:
    if info.get("status") != "success":
        reason = info.get("error_code") or info.get("status")
        raise TransportError("ChatGPT could not provide the file" + (f" ({label(reason)})." if reason else "."))
    return info


class ChatGPTClient:
    def __init__(self, *, codex: str | None = None, timeout: float = 30,
                 auth: CodexAuth | None = None, opener: Any = None):
        self.auth = auth or CodexAuth(codex, timeout)
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self.auth.close()

    def get(self, path: str, params: dict | None = None) -> dict:
        if not _ENDPOINT.fullmatch(path):
            raise TransportError("Unsupported ChatGPT endpoint.")
        query = urllib.parse.urlencode({
            key: str(value).lower() if isinstance(value, bool) else value
            for key, value in (params or {}).items() if value is not None
        })
        content = self._fetch(BACKEND + path + ("?" + query if query else ""),
                              accept="application/json", limit=MAX_BYTES)
        try:
            result = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            raise TransportError("ChatGPT returned invalid JSON.") from None
        if not isinstance(result, dict):
            raise TransportError("ChatGPT returned an unexpected response shape.")
        return result

    def sandbox_file(self, conversation_id: str, message_id: str, path: str) -> dict:
        return _provided(self.get(f"/conversation/{conversation_id}/interpreter/download",
                                  {"message_id": message_id, "sandbox_path": path}))

    def uploaded_file(self, conversation_id: str, file_id: str) -> dict:
        return _provided(self.get(f"/files/download/{file_id}", {"conversation_id": conversation_id}))

    def download(self, url: Any, *, limit: int) -> bytes:
        try:
            parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        except ValueError:
            parts = None
        if (parts is None or (parts.scheme, parts.netloc, parts.path) != ("https", "chatgpt.com", DOWNLOAD_PATH)
                or not parts.query or parts.fragment or not parts.query.isprintable() or " " in parts.query):
            raise TransportError("Refusing to send the ChatGPT token to an unexpected download URL.")
        return self._fetch(BACKEND_ORIGIN + DOWNLOAD_PATH + "?" + parts.query, accept="*/*", limit=limit)

    def _fetch(self, url: str, *, accept: str, limit: int) -> bytes:
        for attempt in range(2):
            token = self.auth.token(refresh=attempt == 1)
            headers = {"Authorization": "Bearer " + token, "Accept": accept,
                       "User-Agent": "cx-chats/0.1.0", "originator": "cx-chats"}
            account = _account_id(token)
            if account:
                headers["ChatGPT-Account-Id"] = account
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    content = response.read(limit + 1)
            except urllib.error.HTTPError as error:
                status = error.code
                error.close()
                if status == 401 and attempt == 0:
                    continue
                raise TransportError(f"ChatGPT request failed (HTTP {status}).") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                raise TransportError("ChatGPT request failed; check network connectivity.") from None
            if len(content) > limit:
                raise TransportError(f"ChatGPT response exceeded the size limit of {limit:,} bytes.")
            return content
        raise TransportError("ChatGPT authentication failed after refresh.")

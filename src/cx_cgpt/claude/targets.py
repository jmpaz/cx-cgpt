from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

_OPTIONS = {
    "chat": {"output", "tool", "result_head_tokens", "result_tail_tokens"},
    "chats": {"after", "before", "limit", "offset", "output"},
}
_INTEGERS = {"limit": 1, "offset": 0, "result_head_tokens": 0, "result_tail_tokens": 0}
_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2}))?\Z"
)
_HANDLE = re.compile(r"t[1-9]\d*\Z")


@dataclass(frozen=True)
class Target:
    kind: str
    conversation_id: str | None
    options: dict[str, Any]

    @property
    def canonical(self) -> str:
        body = f"chat/{self.conversation_id}" if self.kind == "chat" else "chats"
        query = urlencode(sorted(self.options.items()))
        return f"claude:{body}" + (f"?{query}" if query else "")


def is_claude_target(raw: Any) -> bool:
    if not isinstance(raw, str):
        return False
    raw = raw.strip()
    if raw.startswith("claude:"):
        return True
    try:
        url = urlsplit(raw)
    except ValueError:
        return False
    return url.scheme == "https" and url.netloc == "claude.ai" and url.path.startswith("/chat/")


def normalize_options(raw: dict[str, Any] | None, kind: str | None = None) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("claude options must be a mapping")
    allowed = _OPTIONS[kind] if kind else set().union(*_OPTIONS.values())
    unknown = raw.keys() - allowed
    if unknown:
        raise ValueError(f"Unknown claude options: {sorted(unknown)}")
    options = dict(raw)
    for key, minimum in _INTEGERS.items():
        if key not in options:
            continue
        value = options[key]
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"{key} must be an integer")
        try:
            options[key] = int(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if options[key] < minimum:
            raise ValueError(f"{key} must be at least {minimum}")
    if options.get("limit", 1) > 100:
        raise ValueError("limit must be at most 100")
    for key in ("after", "before"):
        if key not in options:
            continue
        value = options[key]
        if not isinstance(value, str) or not _DATE.fullmatch(value):
            raise ValueError(f"{key} must be an ISO date or timezone-aware timestamp")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{key} must be an ISO date or timezone-aware timestamp") from exc
    if "output" in options and options["output"] not in {"transcript", "json"}:
        raise ValueError("output must be transcript or json")
    if "tool" in options and not (isinstance(options["tool"], str) and _HANDLE.fullmatch(options["tool"])):
        raise ValueError("tool must be a tool handle such as t3")
    return options


def parse_target(raw: str, overrides: dict[str, Any] | None = None) -> Target | None:
    if not is_claude_target(raw):
        return None
    raw = raw.strip()
    if raw.startswith("https://"):
        url = urlsplit(raw)
        if url.fragment:
            raise ValueError("claude.ai chat targets do not support fragments")
        body, query = "chat/" + url.path.removeprefix("/chat/").rstrip("/"), url.query
    else:
        body, _, query = raw.removeprefix("claude:").partition("?")
        body = body.strip("/")
    if body == "chats":
        kind, identifier = "chats", None
    else:
        kind = "chat"
        try:
            identifier = str(UUID(body.removeprefix("chat/")))
        except ValueError as exc:
            raise ValueError("claude chat targets require a UUID conversation ID") from exc
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True) if query else []
    if len(dict(pairs)) != len(pairs):
        raise ValueError("Duplicate claude query options are not supported")
    options = normalize_options({**dict(pairs), **normalize_options(overrides)}, kind)
    return Target(kind, identifier, options)

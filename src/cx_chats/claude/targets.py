from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

from ..tools import TOOL_MODES, valid_handles

_OPTIONS = {
    "chat": {"output", "tool", "tools", "file", "files", "result_head_tokens", "result_tail_tokens",
             "result_offset", "result_tokens"},
    "chats": {"after", "before", "limit", "offset", "output"},
    "search": {"query", "project", "after", "before", "limit", "offset", "output"},
}
SEARCH_LIMIT = 200
_INTEGERS = {"limit": 1, "offset": 0, "result_head_tokens": 0, "result_tail_tokens": 0,
             "result_offset": 0, "result_tokens": 1}
_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2}))?\Z"
)


@dataclass(frozen=True)
class Target:
    kind: str
    conversation_id: str | None
    options: dict[str, Any]

    @property
    def canonical(self) -> str:
        body = f"chat/{self.conversation_id}" if self.kind == "chat" else self.kind
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
    most = 100 if kind == "chats" else SEARCH_LIMIT
    if options.get("limit", 1) > most:
        raise ValueError(f"limit must be at most {most}")
    if "query" in options and (not isinstance(options["query"], str) or not options["query"].strip()):
        raise ValueError("query must be nonempty text")
    if "project" in options:
        try:
            options["project"] = str(UUID(str(options["project"])))
        except ValueError as exc:
            raise ValueError("project must be a claude.ai project UUID") from exc
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
    if "tool" in options and not valid_handles(options["tool"]):
        raise ValueError("tool must be a handle such as t3 or a range such as t3-t9")
    if "tools" in options and options["tools"] not in TOOL_MODES:
        raise ValueError("tools must be lines, preview, or none")
    if "files" in options and options["files"] not in {"attach", "inline"}:
        raise ValueError("files must be attach or inline")
    if "file" in options and not (isinstance(options["file"], str) and options["file"].strip()
                                  and not options["file"].startswith("/")):
        raise ValueError("file must be a path the transcript lists, such as outputs/notes.md")
    if "file" in options and "tool" in options:
        raise ValueError("file and tool read different parts of a chat; give one")
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
    if body in ("chats", "search"):
        kind, identifier = body, None
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
    if kind == "search" and "query" not in options:
        raise ValueError("claude search requires query")
    return Target(kind, identifier, options)

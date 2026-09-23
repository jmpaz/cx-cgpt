from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

from ..tools import TOOL_MODES, valid_handles

_TOOL_OPTIONS = {"tool", "tools", "result_head_tokens", "result_tail_tokens", "result_offset", "result_tokens"}
_OPTIONS = {
    "thread": {"output", "file", "files", *_TOOL_OPTIONS},
    "share": {"output", *_TOOL_OPTIONS},
    "threads": {"after", "before", "limit", "offset", "output"},
    "search": {"query", "cursor", "after", "before", "limit", "offset", "output"},
}
_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2}))?\Z"
)


@dataclass(frozen=True)
class Target:
    kind: str
    conversation_id: str | None
    options: dict[str, Any]
    share_id: str | None = None

    @property
    def canonical(self) -> str:
        if self.kind == "thread":
            body = f"thread/{self.conversation_id}"
        elif self.kind == "share":
            body = f"share/{self.share_id}"
        else:
            body = self.kind
        query = urlencode(sorted(self.options.items()))
        return f"chatgpt:{body}" + (f"?{query}" if query else "")


def is_chatgpt_target(raw: str) -> bool:
    if not isinstance(raw, str):
        return False
    raw = raw.strip()
    if raw.startswith(("chatgpt:", "chatgpt-conversation://")):
        return True
    try:
        url = urlsplit(raw)
        if url.scheme != "https":
            return False
        if url.netloc == "chatgpt.com" and url.path.startswith("/c/"):
            return True
        return url.netloc in {"chatgpt.com", "chat.openai.com"} and (
            url.path == "/share" or url.path.startswith("/share/")
        )
    except ValueError:
        return False


def normalize_options(
    raw: dict[str, Any] | None, kind: str | None = None
) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("chatgpt options must be a mapping")
    allowed = _OPTIONS[kind] if kind else set().union(*_OPTIONS.values())
    unknown = raw.keys() - allowed
    if unknown:
        raise ValueError(f"Unknown chatgpt options: {sorted(unknown)}")
    options = dict(raw)
    for key in ("limit", "offset"):
        if key not in options:
            continue
        value = options[key]
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"{key} must be an integer")
        try:
            options[key] = int(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if options[key] < (1 if key == "limit" else 0):
            raise ValueError(
                f"{key} must be {'positive' if key == 'limit' else 'nonnegative'}"
            )
    if options.get("limit", 1) > 100:
        raise ValueError("limit must be at most 100")
    for key, minimum in (("result_head_tokens", 0), ("result_tail_tokens", 0), ("result_offset", 0), ("result_tokens", 1)):
        if key not in options:
            continue
        value = options[key]
        try:
            options[key] = int(value) if not isinstance(value, bool) else None
        except (TypeError, ValueError):
            options[key] = None
        if options[key] is None or options[key] < minimum:
            raise ValueError(f"{key} must be an integer of at least {minimum}")
    if "tool" in options and not valid_handles(options["tool"]):
        raise ValueError("tool must be a handle such as t3 or a range such as t3-t9")
    if "tools" in options and options["tools"] not in TOOL_MODES:
        raise ValueError("tools must be lines, preview, or none")
    if "tool" in options and "file" in options:
        raise ValueError("file and tool read different parts of a conversation; give one")
    if "cursor" in options and (
        not isinstance(options["cursor"], str) or not options["cursor"]
    ):
        raise ValueError("cursor must be nonempty text")
    for key in ("after", "before"):
        if key not in options:
            continue
        value = options[key]
        if not isinstance(value, str) or not _DATE.fullmatch(value):
            raise ValueError(f"{key} must be an ISO date or timezone-aware timestamp")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"{key} must be an ISO date or timezone-aware timestamp"
            ) from exc
    if "output" in options and options["output"] not in {"transcript", "json"}:
        raise ValueError("output must be transcript or json")
    if "query" in options and (
        not isinstance(options["query"], str) or not options["query"].strip()
    ):
        raise ValueError("query must be nonempty text")
    if "files" in options and options["files"] not in {"attach", "inline"}:
        raise ValueError("files must be attach or inline")
    if "file" in options and not (
        isinstance(options["file"], str)
        and options["file"].strip()
        and not options["file"].startswith("/")
    ):
        raise ValueError(
            "file must be a path the transcript lists, such as outputs/notes.md"
        )
    return options


def parse_target(raw: str, overrides: dict[str, Any] | None = None) -> Target | None:
    if not is_chatgpt_target(raw):
        return None
    raw = raw.strip()
    if raw.startswith("https://"):
        url = urlsplit(raw)
        if url.fragment:
            raise ValueError("ChatGPT conversation targets do not support fragments")
        if url.path == "/share" or url.path.startswith("/share/"):
            body = "share/" + (
                url.path.removeprefix("/share/") if url.path != "/share" else ""
            ).rstrip(";").rstrip("/")
        else:
            body = "thread/" + url.path.removeprefix("/c/").rstrip(";").rstrip("/")
        query = url.query
    elif raw.startswith("chatgpt-conversation://"):
        body, _, query = raw.removeprefix("chatgpt-conversation://").partition("?")
        body = "thread/" + body.rstrip(";").rstrip("/")
    else:
        body, _, query = raw.removeprefix("chatgpt:").partition("?")
        body = body.strip("/")
    share_id = None
    if body == "share" or body.startswith("share/"):
        kind, identifier = "share", None
        candidate = body.removeprefix("share/").rstrip(";")
        if candidate.startswith("e/"):
            raise ValueError(
                "Workspace-restricted ChatGPT share URLs are not supported"
            )
        try:
            share_id = str(UUID(candidate))
        except ValueError as exc:
            raise ValueError("ChatGPT share targets require a UUID share ID") from exc
    elif body in {"", "threads"}:
        kind, identifier = "threads", None
    elif body == "search":
        kind, identifier = "search", None
    else:
        kind = "thread"
        candidate = body.removeprefix("thread/")
        try:
            identifier = str(UUID(candidate))
        except ValueError as exc:
            raise ValueError(
                "ChatGPT thread targets require a UUID conversation ID"
            ) from exc
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    if len(dict(pairs)) != len(pairs):
        raise ValueError("Duplicate chatgpt query options are not supported")
    options = normalize_options({**dict(pairs), **normalize_options(overrides)}, kind)
    if kind == "search" and "query" not in options:
        raise ValueError("ChatGPT search requires query")
    return Target(kind, identifier, options, share_id)

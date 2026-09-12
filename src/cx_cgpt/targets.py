from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

_OPTIONS = {
    "thread": {"output"},
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

    @property
    def canonical(self) -> str:
        body = f"thread/{self.conversation_id}" if self.kind == "thread" else self.kind
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
        return (
            url.scheme == "https"
            and url.netloc == "chatgpt.com"
            and url.path.startswith("/c/")
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
    return options


def parse_target(raw: str, overrides: dict[str, Any] | None = None) -> Target | None:
    if not is_chatgpt_target(raw):
        return None
    raw = raw.strip()
    if raw.startswith("https://"):
        url = urlsplit(raw)
        if url.fragment:
            raise ValueError("ChatGPT conversation targets do not support fragments")
        body = "thread/" + url.path.removeprefix("/c/").rstrip(";").rstrip("/")
        query = url.query
    elif raw.startswith("chatgpt-conversation://"):
        body, _, query = raw.removeprefix("chatgpt-conversation://").partition("?")
        body = "thread/" + body.rstrip(";").rstrip("/")
    else:
        body, _, query = raw.removeprefix("chatgpt:").partition("?")
        body = body.strip("/")
    if body in {"", "threads"}:
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
    return Target(kind, identifier, options)

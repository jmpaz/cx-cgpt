from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode


def query_pairs(query: str) -> list[tuple[str, str]]:
    fields = "&".join(field if "=" in field or not field else f"{field}=" for field in query.split("&"))
    return parse_qsl(fields, keep_blank_values=True, strict_parsing=True) if query else []


def query_string(options: dict[str, Any]) -> str:
    return "&".join(name if value == "" else urlencode({name: value}) for name, value in sorted(options.items()))


def with_query(target: str, query: str) -> str:
    return f"{target}{'&' if '?' in target else '?'}{query}"

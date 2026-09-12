from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .targets import Target

MAX_PAGES = 25


def timestamp(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value) if math.isfinite(value) else None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _matches(item: dict, options: dict) -> bool:
    if not ("after" in options or "before" in options):
        return True
    updated = timestamp(item.get("update_time"))
    if updated is None:
        return False
    return ("after" not in options or updated >= timestamp(options["after"])) and (
        "before" not in options or updated < timestamp(options["before"])
    )


def _items(page: dict) -> list[dict]:
    if not isinstance(page, dict):
        raise ValueError("ChatGPT returned an unsupported history page")
    items = page.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("ChatGPT returned an unsupported history page")
    for item in items:
        identifier = item.get("conversation_id", item.get("id"))
        try:
            UUID(identifier)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("ChatGPT history entry has no valid conversation ID") from exc
    return items


def _continuation(target: Target, **changes: Any) -> str:
    options = {**target.options, **changes}
    options = {key: value for key, value in options.items() if value is not None}
    return Target(target.kind, None, options).canonical


def read_page(client: Any, target: Target) -> dict:
    options = target.options
    limit, offset = options.get("limit", 20), options.get("offset", 0)
    after, before = timestamp(options.get("after")), timestamp(options.get("before"))
    if after is not None and before is not None and after >= before:
        raise ValueError("after must be earlier than before")
    selected: list[dict] = []
    scanned = 0
    pages = 0
    next_target = None
    next_offset = None
    total = None
    if target.kind == "threads":
        position = offset
        while pages < MAX_PAGES and len(selected) < limit:
            page = client.get("/conversations", {
                "offset": position, "limit": limit - len(selected),
                "order": "updated", "is_archived": "false", "hide_snorlax": "false",
            })
            pages += 1
            items = _items(page)
            total = page.get("total")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ValueError("ChatGPT listing is missing its total count")
            scanned += len(items)
            position += len(items)
            selected.extend(item for item in items if _matches(item, options))
            has_more = position < total
            if not items and has_more:
                raise ValueError("ChatGPT listing made no pagination progress")
            if not has_more:
                break
            next_offset = position
            next_target = _continuation(target, offset=position)
            if len(selected) >= limit:
                break
        if position >= total:
            next_target = next_offset = None
    else:
        cursor = options.get("cursor")
        skip = offset
        seen = set()
        while pages < MAX_PAGES and len(selected) < limit:
            if cursor in seen:
                raise ValueError("ChatGPT search repeated a pagination cursor")
            seen.add(cursor)
            params = {"query": options["query"]}
            if cursor is not None:
                params["cursor"] = cursor
            page = client.get("/conversations/search", params)
            pages += 1
            items = _items(page)
            following = page.get("cursor")
            if following is not None and not isinstance(following, str):
                raise ValueError("ChatGPT search returned an invalid cursor")
            following = following or None
            if following is not None and following in seen:
                raise ValueError("ChatGPT search repeated a pagination cursor")
            consumed = 0
            for index, item in enumerate(items):
                consumed = index + 1
                if skip:
                    skip -= 1
                    continue
                scanned += 1
                if _matches(item, options):
                    selected.append(item)
                if len(selected) == limit:
                    break
            if consumed < len(items):
                next_target = _continuation(target, cursor=cursor, offset=consumed)
            elif following:
                next_target = _continuation(target, cursor=following, offset=skip)
            else:
                next_target = None
            next_offset = offset + scanned if next_target else None
            if len(selected) == limit or following is None:
                break
            cursor = following
    return {
        "items": selected, "offset": offset, "limit": limit,
        "returned": len(selected), "scanned": scanned, "pages": pages,
        "total_reported": total, "total_is_exact": False, "next_offset": next_offset, "next_target": next_target,
        "scan_limit_reached": pages == MAX_PAGES and len(selected) < limit and next_target is not None,
        "date_field": "update_time", "date_filter": "inclusive after, exclusive before; UTC for dates",
        "source_order": "updated descending" if target.kind == "threads" else "server search order",
        "archived": False if target.kind == "threads" else "included when returned by search",
    }

from __future__ import annotations

from typing import Any
from uuid import UUID

from ..service import timestamp
from .targets import SEARCH_LIMIT, Target

MAX_PAGES = 25
FILTERED_PAGE_SIZE = 50


def _matches(item: dict, after: float | None, before: float | None) -> bool:
    if after is None and before is None:
        return True
    updated = timestamp(item.get("updated_at"))
    if updated is None:
        return False
    return (after is None or updated >= after) and (before is None or updated < before)


def _rows(page: Any) -> tuple[list[dict], bool]:
    rows = page.get("data") if isinstance(page, dict) else None
    has_more = page.get("has_more") if isinstance(page, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) or not isinstance(has_more, bool):
        raise ValueError("claude.ai returned an unsupported chat listing page")
    for row in rows:
        try:
            UUID(row.get("uuid"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("claude.ai chat listing entry has no valid conversation ID") from exc
    return rows, has_more


def read_page(client: Any, target: Target) -> dict:
    options = target.options
    limit, offset = options.get("limit", 20), options.get("offset", 0)
    after, before = timestamp(options.get("after")), timestamp(options.get("before"))
    if after is not None and before is not None and after >= before:
        raise ValueError("after must be earlier than before")
    filtered = after is not None or before is not None
    selected: list[dict] = []
    position, scanned, pages, more = offset, 0, 0, True
    while more and pages < MAX_PAGES and len(selected) < limit:
        size = FILTERED_PAGE_SIZE if filtered else limit - len(selected)
        rows, has_more = _rows(client.conversations(limit=size, offset=position))
        pages += 1
        if not rows and has_more:
            raise ValueError("claude.ai chat listing made no pagination progress")
        consumed = 0
        for row in rows:
            consumed += 1
            if _matches(row, after, before):
                selected.append(row)
                if len(selected) == limit:
                    break
        scanned += consumed
        position += consumed
        more = has_more or consumed < len(rows)
    next_target = None
    if more:
        next_target = Target("chats", None, {**options, "offset": position}).canonical
    return {
        "items": selected, "offset": offset, "limit": limit, "returned": len(selected),
        "scanned": scanned, "pages": pages, "next_offset": position if more else None,
        "next_target": next_target,
        "scan_limit_reached": pages == MAX_PAGES and len(selected) < limit and more,
        "date_field": "updated_at", "date_filter": "inclusive after, exclusive before; UTC for dates",
        "source_order": "updated descending",
    }


def _search_rows(page: Any) -> list[dict]:
    rows = page.get("data") if isinstance(page, dict) else None
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("conversation"), dict) for row in rows
    ):
        raise ValueError("claude.ai returned an unsupported search page")
    for row in rows:
        try:
            UUID(row["conversation"].get("uuid"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("claude.ai search result has no valid conversation ID") from exc
    return rows


def read_search(client: Any, target: Target) -> dict:
    options = target.options
    limit, offset = options.get("limit", 20), options.get("offset", 0)
    after, before = timestamp(options.get("after")), timestamp(options.get("before"))
    if after is not None and before is not None and after >= before:
        raise ValueError("after must be earlier than before")
    filtered = after is not None or before is not None
    requested = SEARCH_LIMIT if filtered else min(SEARCH_LIMIT, offset + limit)
    page = client.search(options["query"], limit=requested, project=options.get("project"))
    rows = _search_rows(page)
    matches = [
        {**row["conversation"], "rank": rank, "snippet": (row.get("matched_snippet") or {}).get("text")}
        for rank, row in enumerate(rows)
        if _matches(row["conversation"], after, before)
    ]
    items = matches[offset: offset + limit]
    end = offset + len(items)
    exhaustive = len(rows) < requested
    more = len(matches) > end or (not exhaustive and requested < SEARCH_LIMIT)
    return {
        "items": items, "offset": offset, "limit": limit, "returned": len(items), "scanned": len(rows),
        "requested": requested, "exhaustive": exhaustive,
        "server_limit_reached": not exhaustive and requested == SEARCH_LIMIT, "pages": 1,
        "next_offset": end if more else None,
        "next_target": Target("search", None, {**options, "offset": end}).canonical if more else None,
        "scan_limit_reached": False,
        "search_id": page.get("search_id"), "executed_mode": page.get("executed_mode"),
        "date_field": "updated_at", "date_filter": "inclusive after, exclusive before; UTC for dates",
        "source_order": "server search rank",
    }

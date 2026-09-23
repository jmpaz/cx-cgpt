import pytest

from cx_chats.claude.service import read_page, read_search
from cx_chats.claude.targets import parse_target


def row(number, updated="2026-09-10T00:00:00Z"):
    return {"uuid": f"{number:08d}-0000-4000-8000-000000000000", "name": f"Chat {number}", "updated_at": updated}


class Pages:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = []

    def conversations(self, *, limit, offset):
        self.calls.append((limit, offset))
        rows, has_more = self.pages.pop(0)
        return {"data": rows, "has_more": has_more}


def test_fills_limit_across_pages_and_continues_from_last_row():
    client = Pages(([row(1), row(2)], True), ([row(3), row(4)], True))
    page = read_page(client, parse_target("claude:chats?limit=3"))
    assert [item["name"] for item in page["items"]] == ["Chat 1", "Chat 2", "Chat 3"]
    assert client.calls == [(3, 0), (1, 2)]
    assert page["next_target"] == "claude:chats?limit=3&offset=3"
    assert page["next_offset"] == 3


def test_last_page_ends_pagination():
    page = read_page(Pages(([row(1)], False)), parse_target("claude:chats?limit=5"))
    assert page["returned"] == 1
    assert page["next_target"] is None


def test_date_filters_use_update_time_and_count_scanned_rows():
    client = Pages((
        [row(1, "2026-09-20T00:00:00Z"), row(2, "2026-08-01T00:00:00Z"), row(3, None), row(4, "2026-09-02T00:00:00Z")],
        False,
    ))
    page = read_page(client, parse_target("claude:chats?after=2026-09-01&before=2026-09-15"))
    assert [item["name"] for item in page["items"]] == ["Chat 4"]
    assert page["scanned"] == 4
    assert client.calls == [(50, 0)]


def test_stopping_mid_page_keeps_unread_rows_reachable():
    client = Pages(([row(1), row(2), row(3)], False))
    page = read_page(client, parse_target("claude:chats?limit=1&after=2026-09-01"))
    assert page["next_target"] == "claude:chats?after=2026-09-01&limit=1&offset=1"


@pytest.mark.parametrize("page", [{"data": "x", "has_more": False}, {"data": [{"uuid": "bad"}], "has_more": False}, {"data": []}])
def test_unsupported_pages_fail_explicitly(page):
    class Client:
        def conversations(self, **_):
            return page

    with pytest.raises(ValueError, match="claude.ai"):
        read_page(Client(), parse_target("claude:chats"))


def test_no_progress_is_detected():
    with pytest.raises(ValueError, match="no pagination progress"):
        read_page(Pages(([], True)), parse_target("claude:chats"))


class Search:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def search(self, query, *, limit, project=None):
        self.calls.append((query, limit, project))
        hits = [{"conversation": row, "matched_snippet": {"text": f"about {row['name']}"}} for row in self.rows[:limit]]
        return {"data": hits, "search_id": "search-1", "executed_mode": "hybrid"}


def test_search_pages_within_top_matches():
    client = Search([row(number) for number in range(1, 8)])
    page = read_search(client, parse_target("claude:search?query=x&limit=2&offset=2"))
    assert [(item["name"], item["rank"], item["snippet"]) for item in page["items"]] == [
        ("Chat 3", 2, "about Chat 3"), ("Chat 4", 3, "about Chat 4"),
    ]
    assert client.calls == [("x", 4, None)]
    assert page["next_target"] == "claude:search?limit=2&offset=4&query=x"
    assert (page["exhaustive"], page["executed_mode"]) == (False, "hybrid")


def test_exhausted_search_has_no_continuation():
    page = read_search(Search([row(1), row(2)]), parse_target("claude:search?query=x&limit=5"))
    assert page["returned"] == 2
    assert page["exhaustive"] is True
    assert page["next_target"] is None


def test_filtered_search_requests_all_top_matches_then_filters():
    client = Search([row(1, "2026-09-20T00:00:00Z"), row(2, "2026-08-01T00:00:00Z"), row(3, "2026-09-21T00:00:00Z")])
    page = read_search(client, parse_target("claude:search?query=x&after=2026-09-01&limit=1"))
    assert client.calls == [("x", 200, None)]
    assert [item["name"] for item in page["items"]] == ["Chat 1"]
    assert page["next_target"] == "claude:search?after=2026-09-01&limit=1&offset=1&query=x"


def test_full_server_limit_is_reported():
    page = read_search(Search([row(number) for number in range(1, 201)]), parse_target("claude:search?query=x&limit=200"))
    assert page["server_limit_reached"] is True
    assert page["next_target"] is None


def test_unsupported_search_page_fails_explicitly():
    class Client:
        def search(self, *_, **__):
            return {"data": [{"conversation": {"uuid": "bad"}}]}

    with pytest.raises(ValueError, match="search result has no valid conversation ID"):
        read_search(Client(), parse_target("claude:search?query=x"))

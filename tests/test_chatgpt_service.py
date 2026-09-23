from unittest.mock import Mock
from uuid import UUID

import pytest

from cx_chats.chatgpt import service
from cx_chats.chatgpt.targets import parse_target


def item(number, updated="2026-09-10T00:00:00Z"):
    return {
        "id": str(UUID(int=number)),
        "title": f"Thread {number}",
        "update_time": updated,
    }


def test_search_partial_page_continuation_has_exact_cursor_offset():
    client = Mock()
    client.get.return_value = {
        "items": [item(i) for i in range(1, 6)],
        "cursor": "next",
    }
    result = service.read_page(
        client, parse_target("chatgpt:search?query=x&limit=2&offset=1&cursor=start")
    )
    assert result["items"] == [item(2), item(3)]
    continuation = parse_target(result["next_target"])
    assert continuation.options["cursor"] == "start"
    assert continuation.options["offset"] == 3
    result2 = service.read_page(client, continuation)
    assert result2["items"] == [item(4), item(5)]
    continuation2 = parse_target(result2["next_target"])
    assert continuation2.options["cursor"] == "next"
    assert continuation2.options["offset"] == 0


def test_search_offset_spans_multiple_pages_without_duplicates():
    client = Mock()
    client.get.side_effect = [
        {"items": [item(1), item(2)], "cursor": "page2"},
        {"items": [item(3), item(4)], "cursor": "page3"},
    ]
    result = service.read_page(
        client, parse_target("chatgpt:search?query=x&limit=1&offset=3")
    )
    assert result["items"] == [item(4)]
    assert parse_target(result["next_target"]).options["cursor"] == "page3"
    assert parse_target(result["next_target"]).options["offset"] == 0


def test_search_filter_scans_until_selected_limit_then_resumes_raw_position():
    client = Mock()
    client.get.side_effect = [
        {"items": [item(1, "2020-01-01"), item(2)], "cursor": "page2"},
        {"items": [item(3, "2020-01-01"), item(4), item(5)], "cursor": None},
    ]
    result = service.read_page(
        client, parse_target("chatgpt:search?query=x&limit=2&after=2026-01-01")
    )
    assert result["items"] == [item(2), item(4)]
    assert result["scanned"] == 4
    assert parse_target(result["next_target"]).options["cursor"] == "page2"
    assert parse_target(result["next_target"]).options["offset"] == 2


def test_filtered_search_scan_bound_yields_resume_target(monkeypatch):
    monkeypatch.setattr(service, "MAX_PAGES", 2)
    client = Mock()
    client.get.side_effect = [
        {"items": [item(1, "2020-01-01")], "cursor": "page2"},
        {"items": [item(2, "2020-01-01")], "cursor": "page3"},
    ]
    result = service.read_page(
        client, parse_target("chatgpt:search?query=x&after=2026-01-01")
    )
    assert result["scan_limit_reached"] is True
    assert result["items"] == []
    assert parse_target(result["next_target"]).options["cursor"] == "page3"


def test_listing_filters_keep_raw_offset_and_unfiltered_total():
    client = Mock()
    client.get.side_effect = [
        {"items": [item(1, "2020-01-01"), item(2)], "total": 4},
        {"items": [item(3)], "total": 4},
    ]
    result = service.read_page(
        client, parse_target("chatgpt:threads?limit=2&after=2026-01-01")
    )
    assert result["items"] == [item(2), item(3)]
    assert result["total_reported"] == 4
    assert result["next_offset"] == 3
    assert client.get.call_args_list[1].args[1]["offset"] == 2
    assert client.get.call_args_list[1].args[1]["limit"] == 1


def test_exhausted_listing_has_no_continuation():
    client = Mock()
    client.get.return_value = {"items": [item(1)], "total": 1}
    result = service.read_page(client, parse_target("chatgpt:threads"))
    assert result["next_target"] is None
    assert result["next_offset"] is None


def test_dates_are_utc_after_inclusive_before_exclusive():
    client = Mock()
    client.get.return_value = {
        "items": [
            item(1, "2026-09-09T20:00:00-04:00"),
            item(2, "2026-09-11T00:00:00Z"),
            item(3, None),
        ],
        "total": 3,
    }
    result = service.read_page(
        client, parse_target("chatgpt:threads?after=2026-09-10&before=2026-09-11")
    )
    assert [entry["id"] for entry in result["items"]] == [item(1)["id"]]
    assert service.timestamp("2026-09-10") == service.timestamp("2026-09-10T00:00:00Z")


@pytest.mark.parametrize(
    "page",
    [
        {"items": [{"id": "invalid"}], "total": 1},
        {"items": [None], "total": 1},
        {"items": [], "total": True},
        {"items": [], "total": -1},
        {"items": [], "total": 1},
    ],
)
def test_invalid_listing_schema_or_progress_fails_explicitly(page):
    client = Mock()
    client.get.return_value = page
    with pytest.raises(ValueError):
        service.read_page(client, parse_target("chatgpt:threads"))


def test_invalid_date_order_fails_before_request():
    client = Mock()
    with pytest.raises(ValueError, match="earlier"):
        service.read_page(
            client, parse_target("chatgpt:threads?after=2026-09-12&before=2026-09-12")
        )
    client.get.assert_not_called()


def test_repeated_search_cursor_fails():
    client = Mock()
    client.get.return_value = {"items": [], "cursor": "repeat"}
    with pytest.raises(ValueError, match="repeated"):
        service.read_page(client, parse_target("chatgpt:search?query=x"))


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_timestamp_is_missing(value):
    assert service.timestamp(value) is None


def test_nonobject_page_is_explicit_schema_error():
    client = Mock()
    client.get.return_value = []
    with pytest.raises(ValueError, match="page"):
        service.read_page(client, parse_target("chatgpt:threads"))


def test_repeated_cursor_at_page_boundary_does_not_publish_loop():
    client = Mock()
    client.get.return_value = {"items": [item(1)], "cursor": "repeat"}
    with pytest.raises(ValueError, match="repeated"):
        service.read_page(
            client, parse_target("chatgpt:search?query=x&cursor=repeat&limit=1")
        )


def test_reported_total_is_not_an_account_census():
    from cx_chats.chatgpt.service import read_page
    from cx_chats.chatgpt.targets import parse_target

    class Client:
        def get(self, path, params):
            return {"items": [{"id": "11111111-1111-1111-1111-111111111111"}], "total": 2}

    page = read_page(Client(), parse_target("chatgpt:threads?limit=1"))
    assert page["total_reported"] == 2
    assert page["total_is_exact"] is False
    assert page["next_offset"] == 1

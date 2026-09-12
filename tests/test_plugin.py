import json

import click
import pytest
from click.testing import CliRunner

from cx_cgpt import plugin, service

ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"


def conversation():
    return {
        "conversation_id": ID, "title": "Example conversation", "current_node": "answer",
        "mapping": {
            "root": {"parent": None, "message": None},
            "question": {"parent": "root", "message": {
                "id": "question", "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["Explain this code."]},
            }},
            "answer": {"parent": "question", "message": {
                "id": "answer", "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["This is a visual instrument."]},
            }},
        },
    }


@pytest.fixture
def fake_client(monkeypatch):
    class Client:
        responses = []
        calls = []
        instances = 0
        closed = 0

        def __init__(self):
            type(self).instances += 1

        def __enter__(self):
            return self

        def __exit__(self, *_):
            type(self).closed += 1

        def get(self, path, params=None):
            self.calls.append((path, params))
            assert self.responses, "Unexpected network request"
            return self.responses.pop(0)

    monkeypatch.setattr(plugin, "ChatGPTClient", Client)
    return Client


@pytest.mark.parametrize("target", [f"chatgpt:{ID}", f"chatgpt:thread/{ID}", f"chatgpt-conversation://{ID}", f"https://chatgpt.com/c/{ID};"])
def test_thread_alias_resolution_preserves_provenance(target, fake_client):
    fake_client.responses = [conversation()]
    item, = plugin.resolve(target, {})
    assert item["source"] == f"chatgpt:thread/{ID}"
    assert item["label"] == "Example conversation"
    assert "Explain this code." in item["content"]
    assert "This is a visual instrument." in item["content"]
    assert item["prose_authors"] == ["user", "assistant"]
    assert item["metadata"]["scope_id"] == ID
    assert item["metadata"]["complete"] is True
    assert fake_client.calls == [(f"/conversation/{ID}", None)]
    assert fake_client.closed == 1


@pytest.mark.parametrize("target", [f"chatgpt:{ID}", "chatgpt:threads", "chatgpt:search?query=moodbox"])
def test_offline_resolution_never_creates_client(target, fake_client):
    with pytest.raises(ValueError, match="offline"):
        plugin.resolve(target, {"cache_only": True})
    assert fake_client.instances == 0


def test_offline_listing_never_creates_client(fake_client):
    with pytest.raises(ValueError, match="offline"):
        plugin.list_targets("chatgpt:threads", {"cache_only": True})
    assert fake_client.instances == 0


def test_thread_listing_and_classification_are_offline(fake_client):
    target = f"chatgpt-conversation://{ID}"
    classified = plugin.classify_target(target, {})
    assert classified["metadata"]["id"] == ID
    assert classified["kind"] == "thread"
    assert plugin.list_targets(target, {"cache_only": True})["targets"] == []
    assert plugin.classify_target("https://example.com/c/other", {}) is None
    assert fake_client.instances == 0


def test_mismatched_server_identifier_fails_and_closes(fake_client):
    payload = conversation()
    payload["conversation_id"] = OTHER_ID
    fake_client.responses = [payload]
    with pytest.raises(ValueError, match="different conversation ID"):
        plugin.resolve(f"chatgpt:{ID}", {})
    assert fake_client.closed == 1


def test_raw_output_override_has_stable_source(fake_client):
    fake_client.responses = [conversation()]
    item, = plugin.resolve(f"chatgpt:{ID}", {"overrides": {"chatgpt": {"output": "json"}}})
    assert json.loads(item["content"])["conversation_id"] == ID
    assert item["source"] == f"chatgpt:thread/{ID}?output=json"


def test_filtered_listing_marks_scanned_and_continuation(fake_client):
    fake_client.responses = [
        {"items": [{"id": OTHER_ID, "title": "Old", "update_time": 0}], "total": 3},
        {"items": [{"id": ID, "title": "Current", "update_time": 1800000000}], "total": 3},
    ]
    item, = plugin.resolve("chatgpt:threads?after=2026-01-01&limit=1", {})
    assert "Current" in item["content"] and "Old" not in item["content"]
    assert "scanned 2 entries" in item["content"]
    assert "Continue: chatgpt:threads?" in item["content"]
    assert "Dates filter update_time" in item["content"]
    assert "offset=2" in item["content"]
    assert item["metadata"]["scanned"] == 2
    assert item["metadata"]["next_offset"] == 2


def test_scan_cap_is_visible(fake_client, monkeypatch):
    monkeypatch.setattr(service, "MAX_PAGES", 1)
    fake_client.responses = [{"items": [{"id": ID, "update_time": 0}], "total": 2}]
    item, = plugin.resolve("chatgpt:threads?after=2026-01-01&limit=1", {})
    assert "Scan limit reached; more history remains." in item["content"]
    assert item["metadata"]["scan_limit_reached"] is True
    assert item["metadata"]["returned"] == 0


def test_list_context_overrides_target_page_size(fake_client):
    fake_client.responses = [{"items": [{"id": ID, "title": "Current"}], "total": 10}]
    result = plugin.list_targets("chatgpt:threads?limit=5", {"list_limit": 1, "list_offset": 3})
    assert fake_client.calls[0][1]["limit"] == 1
    assert fake_client.calls[0][1]["offset"] == 3
    assert result["targets"][0]["target"] == f"chatgpt:thread/{ID}"
    assert result["pagination"]["hasMore"] is True
    assert result["pagination"]["nextOffset"] == 4


def test_search_list_continuation_preserves_page_cursor(fake_client):
    fake_client.responses = [{"items": [{"conversation_id": ID, "title": "Current"}, {"conversation_id": OTHER_ID}], "cursor": "later"}]
    result = plugin.list_targets("chatgpt:search?query=moodbox&cursor=current&limit=1", {})
    assert fake_client.calls[0] == ("/conversations/search", {"query": "moodbox", "cursor": "current"})
    continuation = result["pagination"]["nextTarget"]
    assert "cursor=current" in continuation
    assert "offset=1" in continuation
    assert "query=moodbox" in continuation


def test_cli_options_collect_into_context_overrides(fake_client):
    @click.command()
    def command(**params):
        overrides = plugin.collect_cli_overrides("cat", params)
        item, = plugin.resolve("chatgpt:search?query=original", {"overrides": {"chatgpt": overrides}})
        click.echo(item["source"])

    plugin.register_cli_options("cat", command)
    plugin.register_cli_options("cat", command)
    assert sum("--chatgpt-query" in param.opts for param in command.params) == 1
    fake_client.responses = [{"items": [], "cursor": None}]
    result = CliRunner().invoke(command, ["--chatgpt-query", "moodbox", "--chatgpt-limit", "2", "--chatgpt-output", "json"])
    assert result.exit_code == 0, result.output
    assert "query=moodbox" in result.output
    assert "limit=2" in result.output
    assert "output=json" in result.output
    assert fake_client.calls[0][1]["query"] == "moodbox"
    assert plugin.collect_cli_overrides("list", {"chatgpt_query": "ignored"}) is None

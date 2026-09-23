import json

import click
import pytest
from click.testing import CliRunner

from cx_chats.chatgpt import plugin, service
from cx_chats.http import TransportError

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

        def sandbox_file(self, conversation_id, message_id, path):
            self.calls.append(("sandbox_file", message_id, path))
            if isinstance(self.files[path], Exception):
                raise self.files[path]
            return self.files[path]

        def uploaded_file(self, conversation_id, file_id):
            self.calls.append(("uploaded_file", file_id))
            if isinstance(self.files[file_id], Exception):
                raise self.files[file_id]
            return self.files[file_id]

        def download(self, url, *, limit):
            self.calls.append(("download", url))
            if isinstance(self.downloads[url], Exception):
                raise self.downloads[url]
            return self.downloads[url]

    Client.files = {}
    Client.downloads = {}
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
    assert [segment["text"] for segment in item["metadata"]["segments"]] == [
        "Explain this code.", "This is a visual instrument.",
    ]
    assert item["metadata"]["message_count"] == 2
    assert item["metadata"]["approx_tokens"] == 12
    assert fake_client.calls == [(f"/conversation/{ID}", None)]
    assert fake_client.closed == 1


def sandbox_info(name, mime_type, size=None):
    return {"status": "success", "download_url": f"https://chatgpt.com/backend-api/estuary/content?id={name}&sig=x",
            "file_name": name, "file_size_bytes": size, "mime_type": mime_type, "creation_time": None,
            "metadata": {"file_id": f"file_{name}"}}


def conversation_with_files(fake_client):
    payload = conversation()
    payload["current_node"] = "summary"
    payload["mapping"]["question"]["message"]["metadata"] = {"attachments": [
        {"id": "file_notes", "name": "notes.md", "mime_type": "text/markdown", "size": 6},
        {"id": "file_photo", "name": "photo.png", "mime_type": "image/png", "size": 4096},
    ]}
    payload["mapping"]["answer"]["message"]["content"]["parts"] = [
        "Wrote [the plan](sandbox:/mnt/data/work/plan.md), [a chart](sandbox:/mnt/data/chart.png), "
        "and [the page](sandbox:/mnt/data/index.html)."
    ]
    payload["mapping"]["code"] = {"parent": "answer", "message": {
        "id": "code", "author": {"role": "assistant"}, "recipient": "python",
        "content": {"content_type": "code", "text": "open('sandbox:/mnt/data/scratch.txt')"},
    }}
    payload["mapping"]["summary"] = {"parent": "code", "message": {
        "id": "summary", "author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": ["The [plan](sandbox:/mnt/data/work/plan.md) is final."]},
    }}
    fake_client.responses = [payload]
    fake_client.files = {
        "file_notes": sandbox_info("notes.md", None, 6),
        "/mnt/data/work/plan.md": sandbox_info("plan.md", "text/markdown"),
        "/mnt/data/chart.png": sandbox_info("chart.png", "image/png", 2048),
        "/mnt/data/index.html": sandbox_info("index(3).html", None),
    }
    fake_client.downloads = {
        fake_client.files["file_notes"]["download_url"]: b"notes\n",
        fake_client.files["/mnt/data/work/plan.md"]["download_url"]: b"# Plan\n",
        fake_client.files["/mnt/data/index.html"]["download_url"]: b"<h1>Page</h1>\n",
    }


def test_text_uploads_and_linked_sandbox_files_follow_the_transcript(fake_client):
    conversation_with_files(fake_client)
    transcript, *files = plugin.resolve(f"https://chatgpt.com/c/{ID}", {})
    assert [(file["label"], file["content"]) for file in files] == [
        ("uploads/notes.md", "notes\n"), ("outputs/work/plan.md", "# Plan\n"), ("outputs/index.html", "<h1>Page</h1>\n"),
    ]
    output = files[1]
    assert output["source"] == f"chatgpt:thread/{ID}?file=outputs%2Fwork%2Fplan.md"
    assert output["metadata"]["context_subpath"] == f"chatgpt/{ID}/outputs/work/plan.md"
    assert output["metadata"]["source_url"] == f"https://chatgpt.com/c/{ID}"
    assert output["metadata"]["path"] == "/mnt/data/work/plan.md"
    assert (output["metadata"]["provider"], output["metadata"]["kind"]) == ("chatgpt", "file")
    assert files[0]["metadata"]["attachment_id"] == "file_notes"
    text = transcript["content"]
    assert ("Files following the transcript:\n- uploads/notes.md\n- outputs/work/plan.md\n- outputs/index.html\n\n"
            "Files not included:\n- outputs/chart.png (image/png, 2,048 bytes): not text\n\n") in text
    assert "uploads/photo.png" not in text
    assert "Attachment: photo.png (image/png, 4,096 bytes); not fetched" in text
    assert "Attachment: uploads/notes.md (" in text
    assert "[the plan](outputs/work/plan.md)" in text
    assert text.index("Files following") < text.index("## user")
    assert fake_client.calls[1:] == [
        ("uploaded_file", "file_notes"),
        ("download", fake_client.files["file_notes"]["download_url"]),
        ("sandbox_file", "answer", "/mnt/data/work/plan.md"),
        ("download", fake_client.files["/mnt/data/work/plan.md"]["download_url"]),
        ("sandbox_file", "answer", "/mnt/data/chart.png"),
        ("sandbox_file", "answer", "/mnt/data/index.html"),
        ("download", fake_client.files["/mnt/data/index.html"]["download_url"]),
    ]
    assert transcript["metadata"]["files"][2] == {
        "label": "outputs/chart.png", "origin": "output", "content_type": "image/png", "size": 2048,
        "created": None, "included": False, "omitted": "not text",
    }
    assert transcript["metadata"]["files"][3]["size"] == 14


def test_inline_files_keep_sandbox_links_and_make_no_file_requests(fake_client):
    conversation_with_files(fake_client)
    transcript, = plugin.resolve(f"chatgpt:thread/{ID}?files=inline", {})
    assert "[the plan](sandbox:/mnt/data/work/plan.md)" in transcript["content"]
    assert "Files following" not in transcript["content"]
    assert fake_client.calls == [(f"/conversation/{ID}", None)]


def test_one_file_reads_by_the_label_the_transcript_lists(fake_client):
    conversation_with_files(fake_client)
    output, = plugin.resolve(f"chatgpt:thread/{ID}?file=outputs/work/plan.md", {})
    assert (output["label"], output["content"]) == ("outputs/work/plan.md", "# Plan\n")
    assert fake_client.calls[1:] == [
        ("sandbox_file", "answer", "/mnt/data/work/plan.md"),
        ("download", fake_client.files["/mnt/data/work/plan.md"]["download_url"]),
    ]
    fake_client.calls.clear()
    conversation_with_files(fake_client)
    upload, = plugin.resolve(f"chatgpt:thread/{ID}?file=uploads/notes.md", {})
    assert upload["content"] == "notes\n"
    assert fake_client.calls[1:] == [
        ("uploaded_file", "file_notes"), ("download", fake_client.files["file_notes"]["download_url"]),
    ]
    conversation_with_files(fake_client)
    with pytest.raises(ValueError, match="outputs/chart.png is not included: not text"):
        plugin.resolve(f"chatgpt:thread/{ID}?file=outputs/chart.png", {})
    conversation_with_files(fake_client)
    with pytest.raises(ValueError, match="No file outputs/scratch.txt"):
        plugin.resolve(f"chatgpt:thread/{ID}?file=outputs/scratch.txt", {})


def test_unavailable_files_are_reported_without_failing_the_read(fake_client):
    conversation_with_files(fake_client)
    fake_client.files["/mnt/data/index.html"] = TransportError("ChatGPT could not provide the file (ace_pod_expired).")
    plan_url = fake_client.files["/mnt/data/work/plan.md"]["download_url"]
    fake_client.downloads[plan_url] = TransportError("ChatGPT request failed (HTTP 403).")
    fake_client.files["file_notes"] = TransportError("ChatGPT request failed (HTTP 404).")
    transcript, = plugin.resolve(f"chatgpt:thread/{ID}", {})
    assert ("Files not included:\n"
            "- uploads/notes.md (text/markdown, 6 bytes): download failed: ChatGPT request failed (HTTP 404).\n"
            "- outputs/work/plan.md (text/markdown): download failed: ChatGPT request failed (HTTP 403).\n"
            "- outputs/chart.png (image/png, 2,048 bytes): not text\n"
            "- outputs/index.html: download failed: ChatGPT could not provide the file (ace_pod_expired).\n"
            ) in transcript["content"]


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


def test_listing_entries_carry_title_and_times_without_extra_requests(fake_client):
    fake_client.responses = [{
        "items": [{"id": ID, "title": "Current",
                   "create_time": "2026-05-01T12:00:00.000000+00:00",
                   "update_time": 1800000000}],
        "total": 1,
    }]
    result = plugin.list_targets("chatgpt:threads?limit=1", {})
    metadata = result["targets"][0]["metadata"]
    assert metadata["conversation_id"] == ID
    assert metadata["title"] == "Current"
    assert metadata["source_created"] == "2026-05-01T12:00:00Z"
    assert metadata["source_modified"] == "2027-01-15T08:00:00Z"
    assert len(fake_client.calls) == 1


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


@pytest.mark.parametrize("output", ["transcript", "json"])
def test_public_share_never_uses_codex_and_keeps_separate_identity(monkeypatch, fake_client, output):
    payload = {**conversation(), "backing_conversation_id": OTHER_ID}

    class AnonymousClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, share_id):
            assert share_id == ID
            return payload

    monkeypatch.setattr(plugin, "PublicShareClient", AnonymousClient)
    item, = plugin.resolve(f"https://chatgpt.com/share/{ID}?output={output}", {})
    assert fake_client.instances == 0
    assert item["source"] == f"chatgpt:share/{ID}?output={output}"
    assert item["metadata"]["context_subpath"] == f"chatgpt/shares/{ID}.md"
    assert item["metadata"]["share_id"] == ID
    assert item["metadata"]["backing_conversation_id"] == OTHER_ID
    assert item["metadata"]["capture_scope"] == "published_share_snapshot"
    assert item["metadata"]["authentication"] == "none"
    assert [segment["role"] for segment in item["metadata"]["segments"]] == [
        "user", "assistant",
    ]
    if output == "json":
        assert json.loads(item["content"]) == payload
    else:
        assert "Published share snapshot" in item["content"]
        assert "Explain this code." in item["content"]


def test_public_share_offline_operations_do_not_construct_transport(monkeypatch, fake_client):
    def forbidden():
        pytest.fail("Public transport must not be constructed")

    monkeypatch.setattr(plugin, "PublicShareClient", forbidden)
    target = f"chatgpt:share/{ID}"
    assert plugin.classify_target(target, {})["metadata"]["id"] == ID
    assert plugin.list_targets(target, {"cache_only": True})["targets"] == []
    with pytest.raises(ValueError, match="offline"):
        plugin.resolve(target, {"cache_only": True})
    assert fake_client.instances == 0


def test_continuation_offset_is_not_reset_by_list_context(fake_client):
    fake_client.responses = [{"items": [{"id": ID, "title": "Second page"}], "total": 300}]
    plugin.list_targets("chatgpt:threads?limit=1&offset=100", {"list_limit": 1, "list_offset": 0})
    assert fake_client.calls[0][1]["offset"] == 100


def conversation_with_call():
    payload = conversation()
    mapping = payload["mapping"]
    mapping["call"] = {"parent": "question", "message": {
        "id": "call", "author": {"role": "assistant"}, "recipient": "api_tool.call_tool",
        "content": {"content_type": "code", "text": json.dumps({"path": "/App/link_1/search", "args": {"query": "seeds"}})},
    }}
    mapping["result"] = {"parent": "call", "message": {
        "id": "result", "author": {"role": "tool", "name": "api_tool.call_tool"},
        "content": {"content_type": "code", "text": "found " * 100},
    }}
    mapping["answer"]["parent"] = "result"
    return payload


def test_a_tool_call_reads_in_full_without_file_requests(fake_client):
    fake_client.responses = [conversation_with_call()]
    item, = plugin.resolve(f"chatgpt:thread/{ID}?tool=t1&result_tokens=20", {})
    assert item["content"].startswith("# Example conversation · t1 App:search")
    assert f"Continue: chatgpt:thread/{ID}?tool=t1&result_offset=20&result_tokens=20" in item["content"]
    assert item["metadata"]["context_subpath"] == f"chatgpt/{ID}/t1.md"
    assert fake_client.calls == [(f"/conversation/{ID}", None)]


def test_tool_modes_reach_the_transcript(fake_client):
    fake_client.responses = [conversation_with_call()]
    item, = plugin.resolve(f"chatgpt:thread/{ID}?files=inline&tools=none", {})
    assert "↪ 1 tool call [t1]: App:search" in item["content"]
    assert f"read one in full with chatgpt:thread/{ID}?tool=t1" in item["content"]


@pytest.mark.parametrize("query, message", [
    ("tool=3", "tool must be a handle"),
    ("tool=t4-t2", "tool must be a handle"),
    ("tools=all", "tools must be lines, preview, or none"),
    ("result_tokens=0", "result_tokens must be an integer of at least 1"),
    ("tool=t1&file=outputs/a.md", "file and tool"),
])
def test_invalid_tool_options_fail_before_any_request(fake_client, query, message):
    with pytest.raises(ValueError, match=message):
        plugin.resolve(f"chatgpt:thread/{ID}?{query}", {})
    assert fake_client.calls == []

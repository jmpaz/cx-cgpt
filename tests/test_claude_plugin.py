import json

import click
import pytest
from claude_payloads import CONVERSATION, ROOT, block, conversation, message, tool_turn
from click.testing import CliRunner

from cx_chats.claude import plugin
from cx_chats.http import TransportError


@pytest.fixture
def fake_client(monkeypatch):
    class Client:
        payload = None
        pages = []
        calls = []
        instances = 0

        def __init__(self):
            type(self).instances += 1

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def conversation(self, conversation_id):
            self.calls.append(("conversation", conversation_id))
            return self.payload

        def conversations(self, *, limit, offset):
            self.calls.append(("conversations", limit, offset))
            return self.pages.pop(0)

        def output_files(self, conversation_id):
            self.calls.append(("output_files", conversation_id))
            if isinstance(self.outputs, Exception):
                raise self.outputs
            return self.outputs

        def output_file(self, conversation_id, path, *, limit):
            self.calls.append(("output_file", path))
            return self.downloads[path]

    Client.calls = []
    Client.outputs = {"success": True, "files": [], "files_metadata": []}
    Client.downloads = {}
    monkeypatch.setattr(plugin, "ClaudeClient", Client)
    return Client


def test_chat_url_resolves_to_transcript_with_provenance(fake_client):
    fake_client.payload = conversation(*tool_turn())
    item, = plugin.resolve(f"https://claude.ai/chat/{CONVERSATION}", {})
    assert item["source"] == f"claude:chat/{CONVERSATION}"
    assert item["label"] == "Runtime check"
    assert "↪ Runtime:list [t1]" in item["content"]
    assert item["prose_authors"] == ["user", "assistant"]
    metadata = item["metadata"]
    assert metadata["provider"] == "claude"
    assert metadata["context_subpath"] == f"claude/{CONVERSATION}.md"
    assert metadata["source_url"] == f"https://claude.ai/chat/{CONVERSATION}"
    assert metadata["message_count"] == 2
    assert fake_client.calls == [("conversation", CONVERSATION), ("output_files", CONVERSATION)]


def test_tool_target_reads_one_call_in_full(fake_client):
    fake_client.payload = conversation(*tool_turn())
    item, = plugin.resolve(f"claude:chat/{CONVERSATION}?tool=t2", {})
    assert item["content"].startswith("# Runtime check · t2 Runtime:open")
    assert item["metadata"]["context_subpath"] == f"claude/{CONVERSATION}/t2.md"
    assert item["metadata"]["is_error"] is True


def test_json_output_returns_service_payload(fake_client):
    fake_client.payload = conversation(*tool_turn())
    item, = plugin.resolve(f"claude:{CONVERSATION}?output=json", {})
    assert json.loads(item["content"]) == fake_client.payload


def chat_with_files(fake_client):
    question = message(
        "q", ROOT, "human", [block("text", "2026-09-22T19:59:21Z", text="Here are my notes")],
        attachments=[
            {"id": "att-1", "file_name": "", "file_type": "txt", "file_size": 11, "extracted_content": "pasted text"},
            {"id": "att-2", "file_name": "notes.txt", "file_type": "txt", "file_size": 5, "extracted_content": "notes"},
        ],
    )
    answer = message("a", "q", "assistant", [block("text", "2026-09-22T19:59:30Z", text="Wrote the plan.")])
    fake_client.payload = conversation(question, answer)
    fake_client.outputs = {"success": True, "files": [
        "/mnt/user-data/outputs/plan.md", "/mnt/user-data/outputs/chart.png",
    ], "files_metadata": [
        {"path": "/mnt/user-data/outputs/plan.md", "size": 7, "content_type": "text/plain", "created_at": "2026-09-22T19:59:29Z"},
        {"path": "/mnt/user-data/outputs/chart.png", "size": 2048, "content_type": "image/png"},
    ]}
    fake_client.downloads = {"/mnt/user-data/outputs/plan.md": "# Plan\n".encode()}


def test_text_uploads_and_output_files_follow_the_transcript_as_files(fake_client):
    chat_with_files(fake_client)
    transcript, *files = plugin.resolve(f"https://claude.ai/chat/{CONVERSATION}", {})
    assert [(file["label"], file["content"]) for file in files] == [
        ("uploads/pasted-1.txt", "pasted text"), ("uploads/notes.txt", "notes"), ("outputs/plan.md", "# Plan\n"),
    ]
    assert files[2]["source"] == f"claude:chat/{CONVERSATION}?file=outputs%2Fplan.md"
    assert files[2]["metadata"]["context_subpath"] == f"claude/{CONVERSATION}/outputs/plan.md"
    assert files[2]["metadata"]["kind"] == "file"
    text = transcript["content"]
    assert ("Files following the transcript:\n- uploads/pasted-1.txt\n- uploads/notes.txt\n- outputs/plan.md\n\n"
            "Files not included:\n- outputs/chart.png (image/png, 2,048 bytes): not text") in text
    assert "Attachment: uploads/pasted-1.txt (txt, 11 bytes)\n\nAttachment: uploads/notes.txt (txt, 5 bytes)" in text
    assert "pasted text" not in text
    assert text.index("Files following") < text.index("## user")
    assert ("output_file", "/mnt/user-data/outputs/chart.png") not in fake_client.calls
    assert transcript["metadata"]["files"][3] == {
        "label": "outputs/chart.png", "origin": "output", "content_type": "image/png", "size": 2048,
        "created": None, "included": False, "omitted": "not text",
    }


def test_inline_files_keep_uploads_in_their_message_and_skip_outputs(fake_client):
    chat_with_files(fake_client)
    transcript, = plugin.resolve(f"claude:chat/{CONVERSATION}?files=inline", {})
    assert "Attachment: notes.txt (txt, 5 bytes)\n\n```\nnotes\n```" in transcript["content"]
    assert "Files following" not in transcript["content"]
    assert fake_client.calls == [("conversation", CONVERSATION)]


def test_one_file_reads_by_the_path_the_transcript_lists(fake_client):
    chat_with_files(fake_client)
    output, = plugin.resolve(f"claude:chat/{CONVERSATION}?file=outputs/plan.md", {})
    assert (output["label"], output["content"]) == ("outputs/plan.md", "# Plan\n")
    assert ("output_file", "/mnt/user-data/outputs/chart.png") not in fake_client.calls
    fake_client.calls.clear()
    upload, = plugin.resolve(f"claude:chat/{CONVERSATION}?file=uploads/notes.txt", {})
    assert upload["content"] == "notes"
    assert fake_client.calls == [("conversation", CONVERSATION)]
    with pytest.raises(ValueError, match="outputs/chart.png is not included: not text"):
        plugin.resolve(f"claude:chat/{CONVERSATION}?file=outputs/chart.png", {})
    with pytest.raises(ValueError, match="No file outputs/missing.md"):
        plugin.resolve(f"claude:chat/{CONVERSATION}?file=outputs/missing.md", {})


def test_unlisted_outputs_are_reported_without_failing_the_read(fake_client):
    chat_with_files(fake_client)
    fake_client.outputs = TransportError("claude.ai request failed (HTTP 503).")
    transcript, *files = plugin.resolve(f"claude:chat/{CONVERSATION}", {})
    assert [file["label"] for file in files] == ["uploads/pasted-1.txt", "uploads/notes.txt"]
    assert "Files could not be listed: claude.ai request failed (HTTP 503)." in transcript["content"]


def test_mismatched_conversation_is_rejected(fake_client):
    fake_client.payload = {**conversation(*tool_turn()), "uuid": "55555555-5555-4555-8555-555555555555"}
    with pytest.raises(ValueError, match="different conversation"):
        plugin.resolve(f"claude:chat/{CONVERSATION}", {})


def test_listing_envelope_and_continuation(fake_client):
    fake_client.pages = [{"data": [
        {"uuid": CONVERSATION, "name": "Runtime check", "created_at": "2026-09-22T19:59:00Z", "updated_at": "2026-09-22T20:01:00Z"},
    ], "has_more": True}]
    envelope = plugin.list_targets("claude:chats?limit=1", {})
    entry, = envelope["targets"]
    assert entry["target"] == f"claude:chat/{CONVERSATION}"
    assert entry["metadata"]["source_modified"] == "2026-09-22T20:01:00Z"
    assert envelope["pagination"]["nextTarget"] == "claude:chats?limit=1&offset=1"
    assert fake_client.calls == [("conversations", 1, 0)]


def test_rendered_listing_names_continuation(fake_client):
    fake_client.pages = [{"data": [{"uuid": CONVERSATION, "name": "Runtime check"}], "has_more": True}]
    item, = plugin.resolve("claude:chats?limit=1", {})
    assert f"- Runtime check — claude:chat/{CONVERSATION}" in item["content"]
    assert "Continue: claude:chats?limit=1&offset=1" in item["content"]


@pytest.mark.parametrize("target", [f"claude:chat/{CONVERSATION}", "claude:chats"])
def test_offline_reads_never_create_a_client(target, fake_client):
    with pytest.raises(ValueError, match="offline"):
        plugin.resolve(target, {"cache_only": True})
    assert fake_client.instances == 0


def test_cli_options_become_overrides():
    @click.command()
    def cat(**params):
        click.echo(json.dumps(plugin.collect_cli_overrides("cat", params)))

    plugin.register_cli_options("cat", cat)
    result = CliRunner().invoke(cat, ["--claude-tool", "t3", "--claude-result-head-tokens", "40"])
    assert json.loads(result.output) == {"tool": "t3", "result_head_tokens": 40}


def test_search_renders_snippets_and_continuation(fake_client, monkeypatch):
    def search(self, query, *, limit, project=None):
        self.calls.append(("search", query, limit, project))
        return {"data": [{"conversation": {"uuid": CONVERSATION, "name": "Runtime check"},
                          "matched_snippet": {"text": "…the runtime\nregistry…"}}] * limit}

    monkeypatch.setattr(fake_client, "search", search, raising=False)
    item, = plugin.resolve("claude:search?query=runtime&limit=1", {})
    assert item["content"].startswith("# claude.ai search: runtime")
    assert f"- Runtime check — claude:chat/{CONVERSATION}\n  …the runtime registry…" in item["content"]
    assert "Continue: claude:search?limit=1&offset=1&query=runtime" in item["content"]
    assert fake_client.calls == [("search", "runtime", 1, None)]


def test_continuation_offset_is_not_reset_by_list_context(fake_client):
    fake_client.pages = [{"data": [{"uuid": CONVERSATION, "name": "Runtime check"}], "has_more": False}]
    plugin.list_targets("claude:chats?limit=100&offset=100", {"list_limit": 100, "list_offset": 0})
    assert fake_client.calls == [("conversations", 100, 100)]


def retried():
    question = message("q", ROOT, "human", [block("text", "2026-09-22T19:59:21Z", text="Name it")])
    first = message("a1", "q", "assistant", [block("text", "2026-09-22T19:59:22Z", text="First name")])
    second = message("a2", "q", "assistant", [block("text", "2026-09-22T19:59:30Z", text="Second name")],
                     input_mode="retry")
    return conversation(question, first, second, leaf="a2")


def test_a_version_reads_under_its_own_path(fake_client):
    fake_client.payload = retried()
    item, = plugin.resolve(f"claude:chat/{CONVERSATION}?variant=v1", {})
    assert "First name" in item["content"] and "Second name" not in item["content"]
    assert item["metadata"]["context_subpath"] == f"claude/{CONVERSATION}/v1.md"
    assert item["metadata"]["variant"] == "v1"


def test_the_map_reads_without_files(fake_client):
    fake_client.payload = retried()
    item, = plugin.resolve(f"claude:chat/{CONVERSATION}?map", {})
    assert item["source"] == f"claude:chat/{CONVERSATION}?map"
    assert item["content"].startswith("# Runtime check · map")
    assert item["metadata"]["context_subpath"] == f"claude/{CONVERSATION}/map.md"
    assert fake_client.calls == [("conversation", CONVERSATION)]


@pytest.mark.parametrize("query, error", [
    ("variant=2", "variant must be a version handle"),
    ("variants=all", "variants must be notes, preview, or none"),
    ("map&variant=v1", "map shows the whole conversation"),
    ("map=no", "map takes no value"),
])
def test_invalid_variant_options_fail_before_any_request(fake_client, query, error):
    with pytest.raises(ValueError, match=error):
        plugin.resolve(f"claude:chat/{CONVERSATION}?{query}", {})
    assert fake_client.calls == []


def test_the_map_flag_becomes_an_override():
    @click.command()
    def cat(**params):
        click.echo(json.dumps(plugin.collect_cli_overrides("cat", params)))

    plugin.register_cli_options("cat", cat)
    assert json.loads(CliRunner().invoke(cat, ["--claude-map"]).output) == {"map": True}
    assert json.loads(CliRunner().invoke(cat, ["--claude-variants", "preview"]).output) == {"variants": "preview"}

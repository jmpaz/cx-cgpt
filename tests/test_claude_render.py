import pytest
from claude_payloads import CONVERSATION, ROOT, block, conversation, message, tool_turn

from cx_chats.claude.render import render_chat_map, render_conversation, render_tools


def test_tool_calls_render_in_order_with_handles_and_errors():
    text, metadata, prose, authors = render_conversation(conversation(*tool_turn()))
    assert text.index("> Checking the runtime.") < text.index("[t1]") < text.index("[t2]") < text.index(
        "The registry lists three sources."
    )
    assert '↪ Runtime:list [t1] (`{"ref": "ctx://"}`) → ~4 tokens' in text
    assert '↪ Runtime:open [t2] (`{"ref": "ctx://x", "detail": "titles"}`) → ~9 tokens ✗' in text
    assert "registry listing" not in text
    assert f"read one in full with claude:chat/{CONVERSATION}?tool=t1" in text
    assert "Reasoning: the service returned summaries only" in text
    assert metadata["reasoning"] == "summaries"
    assert [(call["handle"], call["name"], call["is_error"]) for call in metadata["tool_calls"]] == [
        ("t1", "Runtime:list", False), ("t2", "Runtime:open", True),
    ]
    assert metadata["tool_calls"][0]["mcp_server_url"] == "https://runtime.example/mcp"
    assert prose == "What can you see?\n\nThe registry lists three sources."
    assert authors == ["user", "assistant"]


def test_segments_follow_turns_and_name_tools():
    _, metadata, _, _ = render_conversation(conversation(*tool_turn()))
    assert metadata["segments"] == [
        {"index": 0, "role": "user", "text": "What can you see?", "start_time": "2026-09-22T19:59:21Z", "tools": []},
        {"index": 1, "role": "assistant",
         "text": "The registry lists three sources.\n\n[tools: Runtime:list, Runtime:open]",
         "start_time": "2026-09-22T19:59:22Z", "tools": ["Runtime:list", "Runtime:open"]},
    ]
    assert metadata["message_count"] == 2
    assert metadata["complete"] is True


def test_long_results_and_inputs_are_previewed_with_full_read_target():
    question = message("q", ROOT, "human", [block("text", "2026-09-22T19:59:21Z", text="Write it")])
    answer = message("a", "q", "assistant", [
        block("tool_use", "2026-09-22T19:59:23Z", id="toolu_1", name="create_file",
              input={"content": "x" * 2000}, integration_name="Files"),
        block("tool_result", "2026-09-22T19:59:24Z", tool_use_id="toolu_1", name="create_file", is_error=False,
              content=[{"type": "text", "text": "HEAD" + "y" * 4000 + "TAIL"}]),
    ])
    text, *_ = render_conversation(conversation(question, answer), tools="preview", head_tokens=10, tail_tokens=5)
    assert "↪ create_file [t1] · Files\n    input: ~505 tokens; showing head ~10 + tail ~5; omitted ~490" in text
    assert f"    read_full: claude:chat/{CONVERSATION}?tool=t1" in text
    assert "    result: ~1,002 tokens; showing head ~10 + tail ~5; omitted ~987" in text
    assert "    HEADyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy\n" in text
    assert "yyyyyyyyyyyyyyyyTAIL" in text


def test_only_active_branch_is_rendered():
    question = message("q", ROOT, "human", [block("text", "2026-09-22T19:59:21Z", text="Question")])
    first = message("a1", "q", "assistant", [block("text", "2026-09-22T19:59:22Z", text="Retried away")])
    second = message("a2", "q", "assistant", [block("text", "2026-09-22T19:59:23Z", text="Kept answer")])
    text, metadata, _, _ = render_conversation(conversation(question, first, second, leaf="a2"))
    assert "Kept answer" in text
    assert "Retried away" not in text
    assert [entry["message_id"] for entry in metadata["messages"]] == ["q", "a2"]


def test_missing_ancestor_marks_capture_incomplete():
    answer = message("a", "gone", "assistant", [block("text", "2026-09-22T19:59:22Z", text="Orphan")])
    text, metadata, _, _ = render_conversation(conversation(answer))
    assert "Capture status: INCOMPLETE\n\n- Missing ancestor message gone." in text
    assert text.index("Capture status") < text.index("## assistant")
    assert metadata["incomplete_reasons"] == ["Missing ancestor message gone."]


def test_a_complete_capture_ends_with_the_last_message():
    text, *_ = render_conversation(conversation(*tool_turn()))
    assert text.rstrip().endswith("The registry lists three sources.")
    assert "Capture status" not in text


def test_user_attachments_files_and_unknown_blocks_stay_visible():
    question = message(
        "q", ROOT, "human", [block("text", "2026-09-22T19:59:21Z", text="See attached")],
        attachments=[{"file_name": "notes.txt", "file_type": "txt", "file_size": 1200, "extracted_content": "pasted ```body```"}],
        files=[{"file_name": "diagram.png", "file_kind": "image"}],
    )
    answer = message("a", "q", "assistant", [
        block("voice_note", "2026-09-22T19:59:22Z", title="memo"),
        block("text", "2026-09-22T19:59:23Z", text="Read.", citations=[{"details": {"url": "https://example.com/a"}}]),
    ])
    text, *_ = render_conversation(conversation(question, answer))
    assert "Attachment: notes.txt (txt, 1,200 bytes)\n\n````\npasted ```body```\n````" in text
    assert "File: diagram.png (image); bytes not fetched" in text
    assert "Structured content (voice_note):" in text
    assert "Sources:\n- https://example.com/a" in text


def test_tool_view_carries_full_input_output_structured_content_and_meta():
    text, metadata = render_tools(conversation(*tool_turn()), "t1")
    assert text.startswith("# Runtime check · t1 Runtime:list")
    assert "MCP server: https://runtime.example/mcp" in text
    assert '## Input\n\n```json\n{\n  "ref": "ctx://"\n}\n```' in text
    assert "## Output\n\nregistry listing" in text
    assert '## Structured content\n\n```json\n{\n  "rows": 3\n}\n```' in text
    assert "io.modelcontextprotocol/serverInfo" in text
    assert metadata["tool"] == "t1"
    assert metadata["is_error"] is False


def test_tool_view_rejects_unknown_handle():
    with pytest.raises(ValueError, match="No tool call t9 .* it has 2"):
        render_tools(conversation(*tool_turn()), "t9")


def test_unpaired_result_gets_its_own_handle():
    question = message("q", ROOT, "human", [block("text", "2026-09-22T19:59:21Z", text="Go")])
    answer = message("a", "q", "assistant", [
        block("tool_result", "2026-09-22T19:59:24Z", tool_use_id="missing", name="late", is_error=False,
              content=[{"type": "text", "text": "orphaned output"}]),
        block("tool_use", "2026-09-22T19:59:25Z", id="toolu_9", name="pending", input={}),
    ])
    text, metadata, _, _ = render_conversation(conversation(question, answer))
    assert "↪ late [t1] → ~4 tokens" in text
    assert "↪ pending [t2] (`{}`) → no result recorded" in text
    assert [call["result_recorded"] for call in metadata["tool_calls"]] == [True, False]


def test_empty_conversation_says_so():
    payload = {"uuid": CONVERSATION, "name": "", "chat_messages": []}
    text, metadata, _, _ = render_conversation(payload)
    assert text.startswith("# Untitled chat")
    assert "The conversation has no messages." in text
    assert metadata["complete"] is True
    assert metadata["segments"] == []


def test_headings_mark_dictated_messages_and_regenerated_replies():
    question, answer = tool_turn()
    question["input_mode"] = "speech_input"
    answer["input_mode"] = "retry"
    text, metadata, _, _ = render_conversation(conversation(question, answer))
    assert "## user · dictated · 2026-09-22T19:59:21Z" in text
    assert "## assistant · regenerated · 2026-09-22T19:59:22Z" in text
    assert "Model: claude-example (claude.ai records the chat's model, not each reply's)" in text
    assert metadata["segments"][0]["dictated"] is True


def test_tool_calls_can_be_counted_instead_of_listed():
    text, *_ = render_conversation(conversation(*tool_turn()), tools="none")
    assert "↪ 2 tool calls [t1–t2]: Runtime:list, Runtime:open" in text
    assert "[t1] (" not in text
    assert text.index("> Checking the runtime.") < text.index("↪ 2 tool calls") < text.index("The registry lists")


def test_a_range_of_calls_reads_in_full():
    text, metadata = render_tools(conversation(*tool_turn()), "t1-t2")
    assert text.startswith("# Runtime check · t1–t2")
    assert "## t1 · Runtime:list" in text and "## t2 · Runtime:open" in text
    assert "### Output\n\nopen takes detail summary or full" in text
    assert metadata["names"] == ["Runtime:list", "Runtime:open"]
    assert metadata["is_error"] is True


def retried():
    question = message("q", ROOT, "human", [block("text", "2026-09-22T19:59:21Z", text="Name it")])
    first = message("a1", "q", "assistant", [block("text", "2026-09-22T19:59:22Z", text="First name")])
    second = message("a2", "q", "assistant", [block("text", "2026-09-22T19:59:30Z", text="Second name")],
                     input_mode="retry")
    edited = message("q2", ROOT, "human", [block("text", "2026-09-22T20:00:00Z", text="Name it again")])
    return conversation(question, first, second, edited, leaf="a2")


def test_retries_and_edits_are_noted_and_open_by_handle():
    text, metadata, _, _ = render_conversation(retried())
    assert "## user · 2026-09-22T19:59:21Z\n\n⑂ version v1 of this message; others: v2 (2026-09-22T20:00:00Z)" in text
    assert "## assistant · regenerated · 2026-09-22T19:59:30Z\n\n⑂ version v4 of this reply; others: v3 (2026-09-22T19:59:22Z)" in text
    assert [fork["kind"] for fork in metadata["variants"]] == ["edit", "reply"]
    first, *_ = render_conversation(retried(), variant="v3")
    assert "First name" in first and "Second name" not in first
    assert "⑂ version v3 of this reply; others: v4 (current, 2026-09-22T19:59:30Z)" in first
    edited, *_ = render_conversation(retried(), variant="v2")
    assert "Name it again" in edited and "First name" not in edited
    with pytest.raises(ValueError, match="No version v9"):
        render_conversation(retried(), variant="v9")


def test_the_chat_map_starts_each_edit_at_the_root():
    text, _ = render_chat_map(retried())
    assert "●   v1 · 1 user · 2026-09-22T19:59:21Z · Name it" in text
    assert "●     v4 · 2 assistant · regenerated · 2026-09-22T19:59:30Z · Second name" in text
    assert "\n      v3 · 2 assistant · 2026-09-22T19:59:22Z · First name" in text
    assert "    v2 · 1 user · 2026-09-22T20:00:00Z · Name it again" in text


def test_a_turn_that_only_attaches_a_file_keeps_its_place():
    question = message("q", ROOT, "human", [], attachments=[{"file_name": "paste.txt", "file_type": "txt", "file_size": 4532}])
    answer = message("a", "q", "assistant", [block("text", "2026-09-22T19:59:23Z", text="Read it.")])
    _, metadata, prose, _ = render_conversation(conversation(question, answer))
    assert [(segment["role"], segment["text"]) for segment in metadata["segments"]] == [
        ("user", "Attachment: paste.txt (txt, 4,532 bytes)"), ("assistant", "Read it."),
    ]
    assert "paste.txt" not in prose
    assert metadata["render_version"] >= 1

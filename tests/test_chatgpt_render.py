import json

import pytest

from cx_chats.chatgpt.render import render_conversation, render_conversation_map, render_tools


def message(identifier, role, content, **extra):
    return {
        "id": identifier,
        "author": {"role": role},
        "content": {"content_type": "text", "parts": [content]},
        **extra,
    }


def tool_message(identifier, text, **metadata):
    return {
        "id": identifier,
        "author": {"role": "tool", "name": "api_tool.call_tool"},
        "recipient": "all",
        "channel": "commentary",
        "content": {
            "content_type": "code",
            "language": "json",
            "response_format_name": None,
            "text": text,
        },
        "metadata": metadata,
    }


def conversation():
    return {
        "id": "conversation",
        "title": "Example",
        "create_time": 0,
        "update_time": 10,
        "current_node": "answer",
        "mapping": {
            "root": {"id": "root", "parent": None, "message": None},
            "question": {
                "id": "question",
                "parent": "root",
                "message": message("q", "user", "Question"),
            },
            "answer": {
                "id": "answer",
                "parent": "question",
                "message": message(
                    "a", "assistant", "Answer", metadata={"model_slug": "example-model"}
                ),
            },
            "alternate": {
                "id": "alternate",
                "parent": "question",
                "message": message("alt", "assistant", "Alternative"),
            },
        },
    }


def test_active_branch_chronology_and_provenance():
    text, metadata, prose, authors = render_conversation(conversation())
    assert text.index("Question") < text.index("Answer")
    assert "Alternative" not in text
    assert metadata["complete"] is True
    assert metadata["message_count"] == 2
    assert metadata["source_created"] == "1970-01-01T00:00:00Z"
    assert metadata["models"] == ["example-model"]
    assert metadata["messages"][1]["message_id"] == "a"
    assert prose == "Question\n\nAnswer"
    assert authors == ["user", "assistant"]


def test_structured_media_and_attachments_preserved_without_claiming_prose():
    payload = conversation()
    payload["mapping"]["answer"]["message"]["content"]["parts"] = [
        "Caption",
        {
            "content_type": "image_asset_pointer",
            "asset_pointer": "file-service://example",
        },
    ]
    payload["mapping"]["answer"]["message"]["metadata"]["attachments"] = [
        {"name": "report.pdf", "id": "attachment"}
    ]
    text, metadata, prose, _ = render_conversation(payload)
    assert "file-service://example" in text
    assert "report.pdf" in text
    assert "file-service" not in prose
    assert metadata["media_fetched"] is False


def test_a_tool_result_without_its_call_still_reads_as_a_tool_call():
    payload = conversation()
    msg = payload["mapping"]["answer"]["message"]
    msg.update(
        author={"role": "tool", "name": "python"},
        recipient="all",
        channel="analysis",
        content={"content_type": "code", "text": "print(1)", "language": "python"},
    )
    text, metadata, prose, authors = render_conversation(payload)
    assert "↪ python [t1] → ~2 tokens" in text
    assert "print(1)" not in prose
    assert metadata["messages"][1]["recipient"] == "all"
    assert metadata["messages"][1]["channel"] == "analysis"
    assert authors == ["user"]
    full, _ = render_tools(payload, "t1", target="chatgpt:thread/conversation")
    assert "## Output\n\nprint(1)" in full


def tool_exchange():
    payload = conversation()
    payload["mapping"].update(
        {
            "call": {
                "id": "call",
                "parent": "question",
                "message": {
                    "id": "c",
                    "author": {"role": "assistant"},
                    "recipient": "api_tool.call_tool",
                    "content": {
                        "content_type": "code",
                        "language": "json",
                        "response_format_name": None,
                        "text": '{"path": "/Example App/list"}',
                    },
                },
            },
            "empty": {
                "id": "empty",
                "parent": "call",
                "message": tool_message(
                    "e",
                    "",
                    invoked_plugin={},
                    invoked_resource={
                        "resource_uri": "/asdk_app_0001/link_0002/list",
                        "publish_status": "private",
                        "app_name": "Example App",
                    },
                    request_id="00000000-0000-0000-0000-000000000000",
                ),
            },
            "output": {
                "id": "output",
                "parent": "empty",
                "message": tool_message("o", '{"items": 2}'),
            },
        }
    )
    payload["mapping"]["answer"]["parent"] = "output"
    return payload


def test_an_app_call_reads_as_one_line_and_its_kept_output_in_full():
    text, metadata, prose, _ = render_conversation(tool_exchange(), target="chatgpt:thread/conversation")
    assert "↪ Example App:list [t1] → ~3 tokens" in text
    assert "asdk_app_0001" not in text
    assert '{"items": 2}' not in text
    assert "no output kept" not in prose
    assert metadata["segments"][1]["tools"] == ["Example App:list"]
    assert metadata["segments"][1]["text"] == "Answer\n\n[tools: Example App:list]"
    assert metadata["tool_calls"] == [{"handle": "t1", "name": "Example App:list", "integration": "Example App",
                                       "output_kept": True, "start_time": None}]
    full, _ = render_tools(tool_exchange(), "t1", target="chatgpt:thread/conversation")
    assert full.startswith("# Example · t1 Example App:list")
    assert '## Output\n\n{"items": 2}' in full


def test_an_app_call_whose_output_chatgpt_dropped_says_so():
    payload = tool_exchange()
    del payload["mapping"]["output"]
    payload["mapping"]["answer"]["parent"] = "empty"
    text, metadata, _, _ = render_conversation(payload)
    assert "↪ Example App:list [t1] → no output kept by ChatGPT" in text
    assert "ChatGPT keeps no output for app (MCP) and web calls" in text
    assert metadata["tool_calls"][0]["output_kept"] is False
    full, _ = render_tools(payload, "t1", target="chatgpt:thread/conversation")
    assert "## Output\n\nNo output kept by ChatGPT." in full


def test_empty_arguments_are_shown_as_given():
    payload = tool_exchange()
    payload["mapping"]["call"]["message"]["content"]["text"] = '{"path": "/Example App/list", "args": {}}'
    text, *_ = render_conversation(payload)
    assert "↪ Example App:list [t1] (`{}`) → ~3 tokens" in text


def test_extra_result_fields_are_kept_for_the_full_view():
    payload = tool_exchange()
    payload["mapping"]["empty"]["message"]["content"]["result"] = {"status": "expired"}
    full, _ = render_tools(payload, "t1", target="chatgpt:thread/conversation")
    assert '## Structured content\n\n```json\n{\n  "result": {\n    "status": "expired"' in full
    assert "response_format_name" not in full


def test_missing_ancestor_retains_reachable_messages_and_marks_incomplete():
    payload = conversation()
    del payload["mapping"]["question"]
    text, metadata, _, _ = render_conversation(payload)
    assert "Answer" in text
    assert text.index("Capture status: INCOMPLETE") < text.index("## assistant")
    assert not metadata["complete"]
    assert "Missing ancestor" in metadata["incomplete_reasons"][0]


def test_a_complete_capture_ends_with_the_last_message():
    text, *_ = render_conversation(conversation())
    assert text.rstrip().endswith("Answer")
    assert "Capture status" not in text


def test_cycle_terminates_without_duplicating_messages():
    payload = conversation()
    payload["mapping"]["question"]["parent"] = "answer"
    text, metadata, _, _ = render_conversation(payload)
    assert not metadata["complete"]
    assert metadata["message_count"] == 2
    assert "Cycle detected" in text


def test_missing_current_node_does_not_guess_a_branch():
    payload = conversation()
    del payload["current_node"]
    _, metadata, prose, _ = render_conversation(payload)
    assert not metadata["complete"]
    assert prose == ""


def test_no_clipping_and_no_metadata_prose_pollution():
    payload = conversation()
    long_text = "long content " * 20000
    payload["mapping"]["answer"]["message"]["content"]["parts"] = [long_text]
    text, _, prose, _ = render_conversation(payload)
    assert long_text in text
    assert long_text in prose
    assert "Provenance" not in prose


def test_nontext_content_is_retained():
    payload = conversation()
    payload["mapping"]["answer"]["message"]["content"] = {
        "content_type": "execution_output",
        "data": {"result": "```"},
    }
    text, _, prose, _ = render_conversation(payload)
    assert '"result": "```"' in text
    assert "````json" in text
    assert prose == "Question"


def test_missing_parent_field_is_incomplete():
    payload = conversation()
    del payload["mapping"]["root"]["parent"]
    _, metadata, _, _ = render_conversation(payload)
    assert not metadata["complete"]


def test_segments_mirror_turns_with_times_and_counts():
    payload = conversation()
    payload["mapping"]["question"]["message"]["create_time"] = 1700000000
    payload["mapping"]["answer"]["message"]["create_time"] = 1700000060
    _, metadata, _, _ = render_conversation(payload)
    assert metadata["segments"] == [
        {
            "index": 0,
            "role": "user",
            "text": "Question",
            "start_time": "2023-11-14T22:13:20Z",
            "tools": [],
        },
        {
            "index": 1,
            "role": "assistant",
            "text": "Answer",
            "start_time": "2023-11-14T22:14:20Z",
            "tools": [],
            "model": "example-model",
        },
    ]
    assert metadata["message_count"] == 2
    assert metadata["model"] == "example-model"
    assert metadata["approx_tokens"] == 4
    assert "Alternative" not in [segment["text"] for segment in metadata["segments"]]


def test_reasoning_tool_calls_and_results_join_their_assistant_turn():
    payload = conversation()
    payload["mapping"].update(
        {
            "analysis": {
                "id": "analysis",
                "parent": "question",
                "message": message(
                    "an", "assistant", "Weighing options", channel="analysis"
                ),
            },
            "call": {
                "id": "call",
                "parent": "analysis",
                "message": message(
                    "c", "assistant", "print(1)", recipient="python", channel="analysis"
                ),
            },
            "result": {
                "id": "result",
                "parent": "call",
                "message": message(
                    "r", "tool", "1", author={"role": "tool", "name": "python"}
                ),
            },
        }
    )
    payload["mapping"]["answer"]["parent"] = "result"
    _, metadata, _, _ = render_conversation(payload)
    assert [segment["role"] for segment in metadata["segments"]] == [
        "user",
        "assistant",
    ]
    assistant = metadata["segments"][1]
    assert assistant["text"] == "Answer\n\n[tools: python]"
    assert assistant["tools"] == ["python"]
    assert "print(1)" not in assistant["text"]
    assert metadata["message_count"] == 2


def test_thoughts_content_becomes_assistant_reasoning():
    payload = conversation()
    payload["mapping"]["analysis"] = {
        "id": "analysis",
        "parent": "question",
        "message": message("an", "assistant", "ignored"),
    }
    payload["mapping"]["analysis"]["message"]["content"] = {
        "content_type": "thoughts",
        "thoughts": [{"summary": "Checking", "content": "the parts list"}],
    }
    payload["mapping"]["answer"]["parent"] = "analysis"
    transcript, metadata, _, _ = render_conversation(payload)
    assert metadata["segments"][1]["text"] == "Answer"
    assert "the parts list" in transcript


def test_a_turn_with_no_reply_keeps_its_reasoning_as_the_segment():
    payload = conversation()
    payload["mapping"]["answer"]["message"]["content"] = {
        "content_type": "thoughts",
        "thoughts": [{"summary": "Checking", "content": "the parts list"}],
    }
    _, metadata, _, _ = render_conversation(payload)
    assert metadata["segments"][1]["text"] == "Checking\n\nthe parts list"


def test_system_and_hidden_messages_stay_out_of_segments():
    payload = conversation()
    payload["mapping"].update(
        {
            "system": {
                "id": "system",
                "parent": "question",
                "message": message("s", "system", "You are helpful"),
            },
            "first": {
                "id": "first",
                "parent": "system",
                "message": message("f", "assistant", "First answer"),
            },
            "instructions": {
                "id": "instructions",
                "parent": "first",
                "message": message(
                    "i",
                    "user",
                    "Preferred tone",
                    metadata={"is_user_system_message": True},
                ),
            },
        }
    )
    payload["mapping"]["answer"]["parent"] = "instructions"
    _, metadata, _, _ = render_conversation(payload)
    assert [
        (segment["role"], segment["text"]) for segment in metadata["segments"]
    ] == [
        ("user", "Question"),
        ("assistant", "First answer"),
        ("assistant", "Answer"),
    ]
    assert "You are helpful" not in str(metadata["segments"])
    assert "Preferred tone" not in str(metadata["segments"])


def test_hidden_messages_stay_out_of_the_transcript_but_keep_their_provenance():
    payload = conversation()
    payload["mapping"]["answer"]["message"]["metadata"][
        "is_visually_hidden_from_conversation"
    ] = True
    text, metadata, _, _ = render_conversation(payload)
    assert "Answer" not in text
    assert len(metadata["messages"]) == 2
    assert [segment["role"] for segment in metadata["segments"]] == ["user"]
    assert metadata["message_count"] == 1


def test_turn_headings_carry_each_replys_model_and_dictation():
    payload = conversation()
    question = payload["mapping"]["question"]["message"]
    question["create_time"] = 1700000000
    question["metadata"] = {"dictation": True}
    payload["mapping"]["answer"]["message"]["create_time"] = 1700000060
    payload["mapping"]["follow"] = {"id": "follow", "parent": "answer",
                                    "message": message("f", "user", "Again", create_time=1700000120)}
    payload["mapping"]["again"] = {"id": "again", "parent": "follow",
                                   "message": message("g", "assistant", "Second", create_time=1700000180,
                                                      metadata={"model_slug": "other-model"})}
    payload["current_node"] = "again"
    text, metadata, _, _ = render_conversation(payload)
    assert "## user · dictated · 2023-11-14T22:13:20Z\n\nQuestion" in text
    assert "## assistant · example-model · 2023-11-14T22:14:20Z\n\n⑂ version v2 of this reply; others: v1\n\nAnswer" in text
    assert "## user · 2023-11-14T22:15:20Z\n\nAgain" in text
    assert "## assistant · other-model · 2023-11-14T22:16:20Z\n\nSecond" in text
    assert "Models: example-model, other-model" in text
    assert metadata["segments"][0]["dictated"] is True
    assert "dictated" not in metadata["segments"][2]
    assert [segment.get("model") for segment in metadata["segments"][1::2]] == ["example-model", "other-model"]


def test_citation_markers_become_their_links():
    payload = conversation()
    answer = payload["mapping"]["answer"]["message"]
    marker = "\ue200cite\ue202turn1search2\ue201"
    answer["content"]["parts"] = [f"Prices fell. {marker} Stray \ue200entity\ue202x\ue201end."]
    answer["metadata"]["content_references"] = [{
        "matched_text": marker, "start_idx": 13, "end_idx": 13 + len(marker),
        "alt": "([Example](https://example.com/a))",
    }]
    text, _, prose, _ = render_conversation(payload)
    assert "Prices fell. ([Example](https://example.com/a)) Stray end." in text
    assert "\ue200" not in text
    assert "\ue200" not in prose


def test_a_footnote_reference_past_the_end_leaves_the_text_alone():
    payload = conversation()
    answer = payload["mapping"]["answer"]["message"]
    answer["content"]["parts"] = ["I'd keep it."]
    answer["metadata"]["content_references"] = [
        {"matched_text": " ", "start_idx": 12, "end_idx": 13, "type": "sources_footnote", "alt": ""},
    ]
    text, *_ = render_conversation(payload)
    assert "I'd keep it." in text


def test_writing_blocks_become_titled_documents():
    payload = conversation()
    payload["mapping"]["answer"]["message"]["content"]["parts"] = [
        'Intro\n\n:::writing{variant="document" id="1" title="A phrase instrument"}\n# Brief\n\nBuild it.\n:::\n\nAfter'
    ]
    text, _, _, _ = render_conversation(payload)
    assert "Writing block: A phrase instrument\n\n```markdown\n# Brief\n\nBuild it.\n```" in text
    assert ":::" not in text
    assert text.index("Intro") < text.index("Writing block") < text.index("After")


def test_sandbox_links_point_at_attached_outputs():
    payload = conversation()
    payload["mapping"]["answer"]["message"]["content"]["parts"] = ["[Notes](sandbox:/mnt/data/notes.md)"]
    inline, *_ = render_conversation(payload)
    attached, *_ = render_conversation(payload, attach_files=True)
    assert "[Notes](sandbox:/mnt/data/notes.md)" in inline
    assert "[Notes](outputs/notes.md)" in attached


def calls_turn():
    payload = conversation()
    mapping = payload["mapping"]
    parent = "question"
    for index, query in enumerate(["alpha", "beta", "gamma"], 1):
        mapping[f"call{index}"] = {"id": f"call{index}", "parent": parent, "message": {
            "id": f"c{index}", "author": {"role": "assistant"}, "recipient": "api_tool.call_tool",
            "content": {"content_type": "code", "text": json.dumps({"path": "/App/link_1/search", "args": {"query": query}})},
        }}
        mapping[f"result{index}"] = {"id": f"result{index}", "parent": f"call{index}",
                                     "message": tool_message(f"r{index}", ("found " + query + " ") * 40)}
        parent = f"result{index}"
    mapping["answer"]["parent"] = parent
    return payload


def test_tool_modes_trade_detail_for_length():
    lines, *_ = render_conversation(calls_turn(), target="chatgpt:thread/conversation")
    assert '↪ App:search [t1] (`{"query": "alpha"}`) → ~' in lines
    assert "found alpha" not in lines
    assert "read one in full with chatgpt:thread/conversation?tool=t1" in lines
    preview, *_ = render_conversation(calls_turn(), tools="preview", target="chatgpt:thread/conversation")
    assert "found alpha" in preview
    counted, *_ = render_conversation(calls_turn(), tools="none", target="chatgpt:thread/conversation")
    assert "↪ 3 tool calls [t1–t3]: App:search ×3" in counted
    assert "[t2]" not in counted
    assert counted.index("tool calls [t1–t3]") < counted.index("Answer")


def test_a_range_of_calls_reads_in_full_and_long_output_pages():
    full, metadata = render_tools(calls_turn(), "t2-t3", target="chatgpt:thread/conversation")
    assert full.startswith("# Example · t2–t3")
    assert "## t2 · App:search" in full and "## t3 · App:search" in full
    assert "### Output\n\n" + "found beta " * 40 in full
    assert metadata["names"] == ["App:search", "App:search"]
    page, _ = render_tools(calls_turn(), "t1", target="chatgpt:thread/conversation", offset=10, tokens=20)
    assert "Showing tokens ~10–30 of ~120." in page
    assert "Continue: chatgpt:thread/conversation?tool=t1&result_offset=30&result_tokens=20" in page
    with pytest.raises(ValueError, match="No tool call t4-t5"):
        render_tools(calls_turn(), "t4-t5", target="chatgpt:thread/conversation")


def test_a_regenerated_reply_is_noted_and_its_other_version_opens():
    payload = conversation()
    payload["mapping"]["question"]["children"] = ["answer", "alternate"]
    text, metadata, _, _ = render_conversation(payload, target="chatgpt:thread/conversation")
    assert "⑂ version v1 of this reply; others: v2" in text
    assert "Variants: 1 fork, marked ⑂ where they meet this path." in text
    assert "Alternative" not in text
    assert metadata["variants"] == [{"kind": "reply", "versions": [
        {"handle": "v1", "message_id": "answer", "created": None, "model": "example-model", "current": True},
        {"handle": "v2", "message_id": "alternate", "created": None, "model": None, "current": False},
    ]}]
    other, metadata, _, _ = render_conversation(payload, variant="v2", target="chatgpt:thread/conversation")
    assert "Alternative" in other and "\nAnswer" not in other
    assert "Branch: version v2 and its latest continuation, not the current path (chatgpt:thread/conversation)." in other
    assert "⑂ version v2 of this reply; others: v1 (current, example-model)" in other
    assert metadata["variant"] == "v2"


def test_variant_modes_preview_or_hide_the_other_versions():
    preview, *_ = render_conversation(conversation(), variants="preview")
    assert "⑂ version v1 of this reply; others: v2\n    v2: Alternative" in preview
    hidden, *_ = render_conversation(conversation(), variants="none")
    assert "⑂" not in hidden


def test_an_edited_message_is_noted_on_the_user_turn():
    payload = conversation()
    mapping = payload["mapping"]
    mapping["edited"] = {"id": "edited", "parent": "root", "message": message("e", "user", "Question, reworded", create_time=5)}
    mapping["later"] = {"id": "later", "parent": "edited", "message": message("l", "assistant", "Reworded answer", create_time=6)}
    mapping["question"]["message"]["create_time"] = 1
    payload["current_node"] = "later"
    text, *_ = render_conversation(payload)
    assert "## user · 1970-01-01T00:00:05Z\n\n⑂ version v2 of this message; others: v1 (1970-01-01T00:00:01Z)" in text


def test_status_placeholders_are_not_versions():
    payload = conversation()
    del payload["mapping"]["alternate"]
    payload["mapping"]["status"] = {"id": "status", "parent": "question", "message": {
        "id": "s", "author": {"role": "tool", "name": "a8km123"},
        "content": {"content_type": "text", "parts": [""]}, "metadata": {"finished_text": "Worked for 3m"},
    }}
    text, metadata, _, _ = render_conversation(payload)
    assert "⑂" not in text and metadata["variants"] == []


def test_an_assistant_heading_takes_the_time_of_what_the_turn_shows():
    payload = conversation()
    del payload["mapping"]["alternate"]
    payload["mapping"]["answer"]["parent"] = "status"
    payload["mapping"]["answer"]["message"]["create_time"] = 20
    payload["mapping"]["status"] = {"id": "status", "parent": "question", "message": {
        "id": "s", "author": {"role": "tool", "name": "a8km123"}, "create_time": 5,
        "content": {"content_type": "text", "parts": [""]}, "metadata": {"model_slug": "example-model"},
    }}
    text, metadata, _, _ = render_conversation(payload)
    assert "## assistant · example-model · 1970-01-01T00:00:20Z" in text
    assert metadata["segments"][-1]["start_time"] == "1970-01-01T00:00:20Z"


def test_the_map_outlines_every_version():
    payload = conversation()
    payload["mapping"]["follow"] = {"id": "follow", "parent": "alternate", "message": message("f", "user", "And then?")}
    text, metadata = render_conversation_map(payload, "chatgpt:thread/conversation")
    assert text.startswith("# Example · map\n\n1 fork; ● marks the current path.")
    assert "● 1 user · Question" in text
    assert "●   v1 · 2 assistant · example-model · Answer" in text
    assert "    v2 · 2 assistant · Alternative" in text
    assert "      3 user · And then?" in text
    assert metadata["variants"][0]["kind"] == "reply"


def test_voice_mode_keeps_what_was_said_on_both_sides_and_marks_what_was_spoken():
    payload = conversation()
    question = payload["mapping"]["question"]["message"]
    question["create_time"] = 1700000000
    question["content"] = {"content_type": "multimodal_text", "parts": [
        {"content_type": "audio_transcription", "text": "Can you read my chat with Sam", "direction": "in"},
    ]}
    answer = payload["mapping"]["answer"]["message"]
    answer["create_time"] = 1700000010
    answer["metadata"] = {"model_slug": "bidi"}
    answer["content"] = {"content_type": "multimodal_text", "parts": [
        {"content_type": "audio_transcription", "text": "Yes, I can read it.", "direction": "out"},
    ]}
    text, metadata, prose, _ = render_conversation(payload)
    assert "## user · voice · 2023-11-14T22:13:20Z\n\nCan you read my chat with Sam" in text
    assert "## assistant · bidi · 2023-11-14T22:13:30Z" in text
    assert "\n[spoken] Yes, I can read it.\n" in text
    assert [(segment["role"], segment["text"]) for segment in metadata["segments"]] == [
        ("user", "Can you read my chat with Sam"), ("assistant", "Yes, I can read it."),
    ]
    assert metadata["segments"][0]["voice"] is True
    assert "Can you read my chat with Sam" in prose


def test_a_turn_that_only_attaches_a_file_keeps_its_place():
    payload = conversation()
    question = payload["mapping"]["question"]["message"]
    question["create_time"] = 1700000000
    question["content"] = {"content_type": "multimodal_text", "parts": []}
    question["metadata"] = {"attachments": [{"id": "file-1", "name": "notes.txt", "mime_type": "text/plain", "size": 120}]}
    text, metadata, prose, _ = render_conversation(payload)
    assert "## user · 2023-11-14T22:13:20Z\n\nAttachment: notes.txt (text/plain, 120 bytes); not fetched" in text
    assert metadata["segments"][0] == {**metadata["segments"][0], "role": "user", "text": "Attachment: notes.txt (text/plain, 120 bytes); not fetched"}
    assert "notes.txt" not in prose
    assert metadata["render_version"] >= 1

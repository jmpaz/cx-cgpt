from cx_chats.chatgpt.render import render_conversation


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


def test_tool_message_name_recipient_and_code_content():
    payload = conversation()
    msg = payload["mapping"]["answer"]["message"]
    msg.update(
        author={"role": "tool", "name": "python"},
        recipient="all",
        channel="analysis",
        content={"content_type": "code", "text": "print(1)", "language": "python"},
    )
    text, metadata, prose, authors = render_conversation(payload)
    assert "tool (python)" in text
    assert "print(1)" in prose
    assert metadata["messages"][1]["recipient"] == "all"
    assert metadata["messages"][1]["channel"] == "analysis"
    assert authors[-1] == "tool (python)"


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


def test_empty_tool_output_names_the_invoked_app_and_drops_default_fields():
    text, metadata, prose, _ = render_conversation(tool_exchange())
    assert (
        "Tool output not retained in conversation history by the service (Example App)."
        in text
    )
    assert '{"path": "/Example App/list"}' in text
    assert '{"items": 2}' in text
    assert text.count("Additional content fields") == 2
    assert "asdk_app_0001" not in text
    assert "not retained" not in prose
    assert metadata["segments"][1]["tools"] == ["api_tool.call_tool"]
    assert metadata["segments"][1]["text"] == "Answer\n\n[tools: api_tool.call_tool]"


def test_empty_tool_output_without_invoked_metadata_names_the_tool():
    payload = tool_exchange()
    payload["mapping"]["empty"]["message"]["metadata"] = {}
    text, _, _, _ = render_conversation(payload)
    assert (
        "Tool output not retained in conversation history by the service"
        " (api_tool.call_tool)." in text
    )


def test_empty_tool_output_keeps_content_fields_that_carry_information():
    payload = tool_exchange()
    payload["mapping"]["empty"]["message"]["content"]["result"] = {"status": "expired"}
    text, _, _, _ = render_conversation(payload)
    assert '"status": "expired"' in text
    assert "response_format_name" not in text.split("## tool")[1]


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


def test_hidden_message_metadata_is_still_rendered_and_counted_as_provenance():
    payload = conversation()
    payload["mapping"]["answer"]["message"]["metadata"][
        "is_visually_hidden_from_conversation"
    ] = True
    text, metadata, _, _ = render_conversation(payload)
    assert "Answer" in text
    assert len(metadata["messages"]) == 2
    assert [segment["role"] for segment in metadata["segments"]] == ["user"]
    assert metadata["message_count"] == 1

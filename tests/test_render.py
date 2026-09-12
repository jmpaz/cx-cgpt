from cx_cgpt.render import render_conversation


def message(identifier, role, content, **extra):
    return {
        "id": identifier,
        "author": {"role": role},
        "content": {"content_type": "text", "parts": [content]},
        **extra,
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


def test_missing_ancestor_retains_reachable_messages_and_marks_incomplete():
    payload = conversation()
    del payload["mapping"]["question"]
    text, metadata, _, _ = render_conversation(payload)
    assert "Answer" in text
    assert "INCOMPLETE" in text
    assert not metadata["complete"]
    assert "Missing ancestor" in metadata["incomplete_reasons"][0]


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

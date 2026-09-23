import pytest

from cx_chats.claude.targets import is_claude_target, parse_target

ID = "33333333-3333-4333-8333-333333333333"


@pytest.mark.parametrize("raw", [f"https://claude.ai/chat/{ID}", f"https://claude.ai/chat/{ID}/", f"claude:chat/{ID}", f"claude:{ID}"])
def test_chat_forms_share_canonical_target(raw):
    target = parse_target(raw)
    assert (target.kind, target.conversation_id, target.canonical) == ("chat", ID, f"claude:chat/{ID}")


@pytest.mark.parametrize("raw", [
    "https://claude.ai/design/p/abc", "https://claude.ai/project/abc", "http://claude.ai/chat/x",
    "https://chatgpt.com/c/abc", "cass:claude/abc", None,
])
def test_other_addresses_are_not_claimed(raw):
    assert not is_claude_target(raw)


def test_listing_options_are_normalized_and_canonical():
    target = parse_target("claude:chats?limit=5&after=2026-09-01", {"offset": "10"})
    assert target.kind == "chats"
    assert target.options == {"limit": 5, "after": "2026-09-01", "offset": 10}
    assert target.canonical == "claude:chats?after=2026-09-01&limit=5&offset=10"


def test_tool_and_preview_options_apply_to_chats():
    target = parse_target(f"claude:chat/{ID}?tool=t12&result_head_tokens=0")
    assert target.options == {"tool": "t12", "result_head_tokens": 0}


@pytest.mark.parametrize("raw, message", [
    (f"claude:chat/{ID}?tool=12", "tool handle"),
    (f"claude:chat/{ID}?tool=t0", "tool handle"),
    (f"claude:chat/{ID}?limit=5", "Unknown claude options"),
    ("claude:chats?tool=t1", "Unknown claude options"),
    ("claude:chats?limit=101", "at most 100"),
    ("claude:chats?limit=0", "at least 1"),
    ("claude:chats?after=2026-09-01T10:00", "timezone-aware"),
    ("claude:chats?limit=1&limit=2", "Duplicate"),
    ("claude:chat/not-a-uuid", "UUID"),
    (f"https://claude.ai/chat/{ID}#top", "fragments"),
])
def test_invalid_targets_fail_explicitly(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_target(raw)


def test_search_requires_query_and_accepts_project_scope():
    target = parse_target("claude:search?query=mcp+debugging&project=66666666-6666-4666-8666-666666666666&limit=150")
    assert target.kind == "search"
    assert target.options == {"query": "mcp debugging", "project": "66666666-6666-4666-8666-666666666666", "limit": 150}
    assert target.canonical == (
        "claude:search?limit=150&project=66666666-6666-4666-8666-666666666666&query=mcp+debugging"
    )
    with pytest.raises(ValueError, match="requires query"):
        parse_target("claude:search")
    with pytest.raises(ValueError, match="at most 200"):
        parse_target("claude:search?query=x&limit=201")
    with pytest.raises(ValueError, match="project UUID"):
        parse_target("claude:search?query=x&project=abc")
    assert parse_target("claude:search?query=x", {"limit": 150}).options["limit"] == 150

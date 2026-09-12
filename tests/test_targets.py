import pytest

from cx_cgpt.targets import is_chatgpt_target, normalize_options, parse_target

ID = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize(
    "raw",
    [
        f"chatgpt:{ID}",
        f"chatgpt:thread/{ID}",
        f"chatgpt-conversation://{ID}",
        f"https://chatgpt.com/c/{ID}",
        f"https://chatgpt.com/c/{ID};",
        f"  https://chatgpt.com/c/{ID}  ",
    ],
)
def test_thread_aliases_have_one_identity(raw):
    target = parse_target(raw)
    assert target.kind == "thread"
    assert target.conversation_id == ID
    assert target.canonical == f"chatgpt:thread/{ID}"


@pytest.mark.parametrize(
    "raw",
    [
        "https://chatgpt.com.evil.test/c/" + ID,
        "https://chatgpt.com@evil.test/c/" + ID,
        "https://chatgpt.com/share/" + ID,
        "http://chatgpt.com/c/" + ID,
        "cass:codex/" + ID,
    ],
)
def test_does_not_claim_other_sources(raw):
    assert not is_chatgpt_target(raw)
    assert parse_target(raw) is None


def test_search_options_and_overrides():
    target = parse_target("chatgpt:search?query=water+music&limit=5", {"limit": 2})
    assert target.options == {"query": "water music", "limit": 2}
    assert parse_target(target.canonical) == target


def test_dates_and_pagination():
    target = parse_target(
        "chatgpt:threads?after=2026-01-01&before=2026-09-12T00:00:00Z&offset=2&limit=20"
    )
    assert target.options["offset"] == 2
    assert target.options["limit"] == 20
    assert parse_target("chatgpt:").kind == "threads"


@pytest.mark.parametrize(
    "raw",
    [
        "chatgpt:thread/not-a-uuid",
        "chatgpt:unknown",
        "chatgpt:search",
        "chatgpt:search?query=",
        "chatgpt:threads?limit=0",
        "chatgpt:threads?offset=-1",
        "chatgpt:threads?limit=2.5",
        "chatgpt:threads?limit=2&limit=3",
        "chatgpt:threads?after=2026-02-30",
        "chatgpt:threads?after=2026-01-01T12:30",
        "chatgpt:threads?unknown=1",
        "chatgpt:threads?query=x",
        "chatgpt:threads?output=csv",
        f"chatgpt:thread/{ID}?limit=2",
        f"https://chatgpt.com/c/{ID}#branch",
        f"https://chatgpt.com/c/{ID}/extra",
        "chatgpt:threads?limit",
    ],
)
def test_invalid_targets_fail_explicitly(raw):
    with pytest.raises(ValueError):
        parse_target(raw)


@pytest.mark.parametrize("value", [True, False, 1.5, None, []])
def test_invalid_integer_overrides(value):
    with pytest.raises(ValueError, match="integer"):
        normalize_options({"limit": value})


def test_output_is_universal():
    assert parse_target(f"chatgpt:{ID}?output=json").options == {"output": "json"}


def test_opaque_search_cursor_roundtrips():
    target = parse_target("chatgpt:search?query=x&cursor=opaque%2B%2F%3D")
    assert target.options["cursor"] == "opaque+/="
    assert parse_target(target.canonical) == target


@pytest.mark.parametrize(
    "raw",
    [
        "chatgpt:threads?limit=101",
        "chatgpt:search?query=x&cursor=",
        "chatgpt:threads?cursor=abc",
    ],
)
def test_search_cursor_and_backend_limit_validation(raw):
    with pytest.raises(ValueError):
        parse_target(raw)

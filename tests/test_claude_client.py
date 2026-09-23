import io
import json
import urllib.error

import pytest

from cx_chats.claude.client import ClaudeClient
from cx_chats.claude.session import Session
from cx_chats.http import TransportError

ORG = "44444444-4444-4444-8444-444444444444"
ID = "33333333-3333-4333-8333-333333333333"


class FakeOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response)


def http_error(status, body, content_type="application/json"):
    return urllib.error.HTTPError("url", status, "error", {"Content-Type": content_type}, io.BytesIO(body))


def client(opener, organization=ORG):
    return ClaudeClient(session=lambda: Session("sk-secret", organization, "chrome:/profile"), opener=opener)


def test_conversation_request_carries_session_and_full_tool_rendering():
    opener = FakeOpener(b'{"uuid": "x"}')
    with client(opener) as claude:
        assert claude.conversation(ID) == {"uuid": "x"}
    request, = opener.requests
    assert request.full_url == (
        f"https://claude.ai/api/organizations/{ORG}/chat_conversations/{ID}"
        "?tree=True&rendering_mode=messages&render_all_tools=true"
    )
    assert request.get_header("Cookie") == "sessionKey=sk-secret"
    assert request.get_method() == "GET"


def test_listing_request_uses_offset_pages():
    opener = FakeOpener(b'{"data": [], "has_more": false}')
    client(opener).conversations(limit=5, offset=10)
    assert opener.requests[0].full_url.endswith("/chat_conversations_v2?limit=5&offset=10&consistency=eventual")


def test_output_files_are_listed_and_downloaded_as_bytes_within_a_limit():
    opener = FakeOpener(b'{"success": true, "files": []}', b"# Plan\n", b"x" * 11)
    claude = client(opener)
    assert claude.output_files(ID) == {"success": True, "files": []}
    assert claude.output_file(ID, "/mnt/user-data/outputs/plan.md", limit=10) == b"# Plan\n"
    listing, download = opener.requests
    assert listing.full_url == f"https://claude.ai/api/organizations/{ORG}/conversations/{ID}/wiggle/list-files"
    assert download.full_url.endswith(f"/conversations/{ID}/wiggle/download-file?path=%2Fmnt%2Fuser-data%2Foutputs%2Fplan.md")
    assert download.get_header("Accept") == "*/*"
    with pytest.raises(TransportError, match="size limit"):
        claude.output_file(ID, "/mnt/user-data/outputs/big.md", limit=10)


@pytest.mark.parametrize("path", [
    f"/api/organizations/{ORG}/chat_conversations/{ID}/completion",
    f"/api/organizations/{ORG}/conversations/{ID}/wiggle/upload-file",
    f"/api/organizations/{ORG}",
    "/api/auth/logout",
])
def test_only_read_endpoints_are_reachable(path):
    opener = FakeOpener()
    with pytest.raises(TransportError, match="Unsupported"):
        client(opener).get(path)
    assert opener.requests == []


def test_service_error_message_is_reported_with_sign_in_hint():
    body = json.dumps({"error": {"type": "permission_error", "message": "Invalid authorization",
                                 "details": {"error_code": "account_session_invalid"}}}).encode()
    with pytest.raises(TransportError) as error:
        client(FakeOpener(http_error(403, body))).conversation(ID)
    message = str(error.value)
    assert "HTTP 403: Invalid authorization account_session_invalid" in message
    assert "sign in to claude.ai again" in message
    assert "sk-secret" not in message


def test_not_found_suggests_organization_override():
    with pytest.raises(TransportError, match="CX_CLAUDE_ORG"):
        client(FakeOpener(http_error(404, b'{"error": {"message": "Not found"}}'))).conversation(ID)


def test_challenge_pages_are_distinguished():
    with pytest.raises(TransportError, match="non-JSON page"):
        client(FakeOpener(http_error(403, b"<html>", "text/html"))).conversation(ID)


def test_missing_organization_is_explicit():
    with pytest.raises(TransportError, match="CX_CLAUDE_ORG"):
        client(FakeOpener(), organization=None).conversation(ID)


def test_search_request_names_query_size_and_project():
    opener = FakeOpener(b'{"data": []}')
    client(opener).search("mcp debugging", limit=20, project="66666666-6666-4666-8666-666666666666")
    assert opener.requests[0].full_url == (
        f"https://claude.ai/api/organizations/{ORG}/conversation/search/v2"
        "?query=mcp+debugging&n=20&target_snippet_size=200&project_uuid=66666666-6666-4666-8666-666666666666"
    )

import base64
import io
import json
import sys
import urllib.error

import pytest

from cx_chats.chatgpt.files import outputs
from cx_chats.chatgpt.transport import ChatGPTClient, CodexAuth
from cx_chats.http import NoRedirect, TransportError


class FakeAuth:
    def __init__(self):
        self.calls = []
        self.closed = False

    def token(self, refresh=False):
        self.calls.append(refresh)
        body = base64.urlsafe_b64encode(json.dumps({
            "https://api.openai.com/auth": {"chatgpt_account_id": "account-example"}
        }).encode()).decode().rstrip("=")
        return "header." + body + ".secret"

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response)


def test_retry_401_and_account_header():
    auth = FakeAuth()
    opener = FakeOpener([
        urllib.error.HTTPError("url", 401, "secret", {}, io.BytesIO(b"secret")), b'{"items": []}'
    ])
    with ChatGPTClient(auth=auth, opener=opener) as client:
        assert client.get("/conversations", {"is_archived": False, "cursor": None}) == {"items": []}
    assert auth.calls == [False, True]
    assert auth.closed
    assert opener.requests[0].get_header("Chatgpt-account-id") == "account-example"
    assert opener.requests[0].full_url == "https://chatgpt.com/backend-api/conversations?is_archived=false"


def test_errors_redact_server_content():
    auth = FakeAuth()
    opener = FakeOpener([urllib.error.HTTPError("secret-url", 403, "secret-message", {}, io.BytesIO(b"secret"))])
    with ChatGPTClient(auth=auth, opener=opener) as client:
        with pytest.raises(TransportError, match=r"HTTP 403") as caught:
            client.get("/conversations")
    assert "secret" not in str(caught.value)
    assert auth.calls == [False]


@pytest.mark.parametrize("path", [
    "https://evil.example/conversations", "/conversations/../account", "/conversation/not-a-uuid", "/conversations?x=1",
    "/files/download/file_1/../account", "/files/download/account", "/estuary/content",
])
def test_endpoint_allowlist_precedes_auth(path):
    auth = FakeAuth()
    with ChatGPTClient(auth=auth) as client:
        with pytest.raises(TransportError, match="Unsupported"):
            client.get(path)
    assert auth.calls == []


CONVERSATION = "11111111-1111-4111-8111-111111111111"
DOWNLOAD = "https://chatgpt.com/backend-api/estuary/content?id=file_1&sig=abc"


def test_sandbox_file_reads_its_info_then_its_bytes_with_the_same_headers():
    info = {"status": "success", "download_url": DOWNLOAD, "mime_type": "text/markdown"}
    opener = FakeOpener([json.dumps(info).encode(), b"# Plan\n"])
    with ChatGPTClient(auth=FakeAuth(), opener=opener) as client:
        assert client.sandbox_file(CONVERSATION, "message", "/mnt/data/plan.md") == info
        assert client.download(info["download_url"], limit=100) == b"# Plan\n"
    described, fetched = opener.requests
    assert described.full_url == (f"https://chatgpt.com/backend-api/conversation/{CONVERSATION}/interpreter/download"
                                  "?message_id=message&sandbox_path=%2Fmnt%2Fdata%2Fplan.md")
    assert fetched.full_url == DOWNLOAD
    assert fetched.get_header("Authorization") == described.get_header("Authorization")
    assert fetched.get_header("Chatgpt-account-id") == "account-example"
    assert fetched.get_header("User-agent") == "cx-chats/0.1.0"


def test_uploaded_file_reads_its_info_by_file_id():
    opener = FakeOpener([json.dumps({"status": "success", "download_url": DOWNLOAD}).encode()])
    with ChatGPTClient(auth=FakeAuth(), opener=opener) as client:
        assert client.uploaded_file(CONVERSATION, "file_00ab")["download_url"] == DOWNLOAD
    assert opener.requests[0].full_url == (
        f"https://chatgpt.com/backend-api/files/download/file_00ab?conversation_id={CONVERSATION}"
    )


def test_sandbox_file_errors_name_the_service_reason():
    opener = FakeOpener([b'{"status": "error", "error_code": "ace_pod_expired", "error_message": null}'])
    with ChatGPTClient(auth=FakeAuth(), opener=opener) as client:
        with pytest.raises(TransportError, match=r"could not provide the file \(ace_pod_expired\)"):
            client.sandbox_file(CONVERSATION, "message", "/mnt/data/plan.md")


def test_download_stops_reading_at_the_limit():
    with ChatGPTClient(auth=FakeAuth(), opener=FakeOpener([b"x" * 11])) as client:
        with pytest.raises(TransportError, match="size limit of 10 bytes"):
            client.download(DOWNLOAD, limit=10)


@pytest.mark.parametrize("url", [
    None,
    "https://evil.example/backend-api/estuary/content?id=file_1",
    "https://chatgpt.com.evil.example/backend-api/estuary/content?id=file_1",
    "https://user@chatgpt.com/backend-api/estuary/content?id=file_1",
    "https://chatgpt.com:8443/backend-api/estuary/content?id=file_1",
    "http://chatgpt.com/backend-api/estuary/content?id=file_1",
    "https://chatgpt.com/backend-api/estuary/content",
    "https://chatgpt.com/backend-api/estuary/content/../files?id=file_1",
    "https://chatgpt.com/backend-api/conversations?id=file_1",
    "https://chatgpt.com/backend-api/estuary/content?id=file_1#fragment",
    "https://chatgpt.com/backend-api/estuary/content?id=file 1",
])
def test_download_refuses_other_urls_before_auth(url):
    auth = FakeAuth()
    with ChatGPTClient(auth=auth, opener=FakeOpener([])) as client:
        with pytest.raises(TransportError, match="unexpected download URL"):
            client.download(url, limit=10)
    assert auth.calls == []


def test_foreign_download_url_leaves_the_file_out_without_sending_the_token():
    payload = {"current_node": "answer", "mapping": {
        "answer": {"parent": None, "message": {
            "id": "answer", "author": {"role": "assistant"},
            "content": {"content_type": "text", "parts": ["[plan](sandbox:/mnt/data/plan.md)"]},
        }},
    }}
    info = {"status": "success", "download_url": "https://evil.example/estuary/content?id=file_1"}
    opener = FakeOpener([json.dumps(info).encode()])
    with ChatGPTClient(auth=FakeAuth(), opener=opener) as client:
        file, = outputs(client, payload, CONVERSATION)
    assert file.content is None
    assert file.omitted == "download failed: Refusing to send the ChatGPT token to an unexpected download URL."
    assert [request.host for request in opener.requests] == ["chatgpt.com"]


def test_redirects_rejected():
    assert NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.example") is None


def test_auth_login_error_redacted(monkeypatch):
    auth = CodexAuth()
    monkeypatch.setattr(auth, "_start", lambda: None)
    monkeypatch.setattr(auth, "_request", lambda *args: {"authMethod": "apikey", "authToken": "secret"})
    with pytest.raises(TransportError, match="Sign in") as caught:
        auth.token()
    assert "secret" not in str(caught.value)


def test_stdio_auth_lifecycle(tmp_path):
    script = tmp_path / "codex"
    script.write_text('#!' + sys.executable + '''
import json,sys
for line in sys.stdin:
    value=json.loads(line)
    if "id" not in value: continue
    result={"authMethod":"chatgpt","authToken":"fake-token"} if value["method"]=="getAuthStatus" else {}
    print(json.dumps({"id":value["id"],"result":result}),flush=True)
''')
    script.chmod(0o755)
    auth = CodexAuth(str(script), timeout=2)
    try:
        assert auth.token() == "fake-token"
        process = auth._process
    finally:
        auth.close()
    assert process.poll() == 0


def test_stdio_timeout_cleanup(tmp_path):
    script = tmp_path / "codex"
    script.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(10)\n')
    script.chmod(0o755)
    auth = CodexAuth(str(script), timeout=.05)
    with pytest.raises(TransportError, match="timed out"):
        auth.token()
    assert auth._process is None

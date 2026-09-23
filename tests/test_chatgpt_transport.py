import base64
import io
import json
import sys
import urllib.error

import pytest

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


@pytest.mark.parametrize("path", ["https://evil.example/conversations", "/conversations/../account", "/conversation/not-a-uuid", "/conversations?x=1"])
def test_endpoint_allowlist_precedes_auth(path):
    auth = FakeAuth()
    with ChatGPTClient(auth=auth) as client:
        with pytest.raises(TransportError, match="Unsupported"):
            client.get(path)
    assert auth.calls == []


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

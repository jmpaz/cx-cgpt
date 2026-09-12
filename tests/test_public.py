import io
import json
import urllib.error

import pytest

from cx_cgpt import public
from cx_cgpt.public import PublicShareClient, PublicShareError, parse_share_html

ID = "11111111-2222-4333-8444-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def conversation():
    return {
        "conversation_id": ID, "title": "Synthetic public example", "is_public": True,
        "current_node": "answer", "backing_conversation_id": OTHER,
        "mapping": {
            "root": {"parent": None, "message": None},
            "answer": {"parent": "root", "message": {
                "id": "answer", "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["A synthetic answer."]},
            }},
        },
    }


def table_for(value):
    table = []

    def encode(value):
        if value is None:
            return -5
        index = len(table)
        table.append(None)
        if isinstance(value, dict):
            encoded = {f"_{encode(key)}": encode(child) for key, child in value.items()}
        elif isinstance(value, list):
            encoded = [encode(child) for child in value]
        else:
            encoded = value
        table[index] = encoded
        return index

    assert encode(value) == 0
    return table


def page(payload=None, *, share_id=ID):
    root = {"loaderData": {"routes/share.$shareId.($action)": {
        "sharedConversationId": share_id,
        "serverResponse": {"type": "data", "data": payload or conversation()},
    }}, "actionData": None}
    encoded = json.dumps(json.dumps(table_for(root)))
    return '<html><script nonce="abc">window.__reactRouterContext.streamController.enqueue(' + encoded + ');</script></html>'


def test_modern_page_preserves_mapping_and_share_provenance():
    payload = parse_share_html(page(), ID)
    assert payload == conversation()
    assert payload["backing_conversation_id"] == OTHER
    assert payload["mapping"]["root"]["parent"] is None


def test_split_stream_chunks_and_scripts():
    html = page()
    start = html.index(public._STREAM_CALL) + len(public._STREAM_CALL)
    stream, _ = json.JSONDecoder().raw_decode(html[start:])
    midpoint = len(stream) // 2
    html = ''.join('<script>' + public._STREAM_CALL + json.dumps(part) + ');</script>' for part in (stream[:midpoint], stream[midpoint:]))
    assert parse_share_html(html, ID)["title"] == conversation()["title"]


def test_legacy_next_data():
    legacy = {"props": {"pageProps": {"serverResponse": {"type": "data", "data": conversation()}}}}
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(legacy) + '</script>'
    assert parse_share_html(html, ID) == conversation()


@pytest.mark.parametrize("payload", [
    {**conversation(), "is_public": False},
    {**conversation(), "mapping": {}},
    {**conversation(), "current_node": "missing"},
])
def test_rejects_inaccessible_mismatched_or_incomplete_shares(payload):
    with pytest.raises(PublicShareError):
        parse_share_html(page(payload), ID)


def test_route_share_id_checked():
    with pytest.raises(PublicShareError, match="different share"):
        parse_share_html(page(share_id=OTHER), ID)


@pytest.mark.parametrize("table", [
    [{"_1": 999}, "loaderData"],
    [{"_1": 0}, "cycle"],
    [{"bad": 1}, "value"],
    [{"_1": -42}, "value"],
    [["unexpected-tag"]],
])
def test_invalid_hydration_reference_tables_fail(table):
    html = '<script>' + public._STREAM_CALL + json.dumps(json.dumps(table)) + ');</script>'
    with pytest.raises(PublicShareError):
        parse_share_html(html, ID)


def test_no_script_execution():
    html = '<script>' + public._STREAM_CALL + '__import__("os").system("false"));</script>'
    with pytest.raises(PublicShareError, match="invalid hydration JSON"):
        parse_share_html(html, ID)


def test_ordinary_html_and_error_pages_rejected():
    with pytest.raises(PublicShareError, match="no supported"):
        parse_share_html('<html><h1>Please sign in</h1></html>', ID)


class Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return io.BytesIO(self.response)


def test_anonymous_request_has_no_auth_or_cookies(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-use')
    monkeypatch.setenv('CX_CHATGPT_CODEX', '/nonexistent/must-not-use')
    opener = Opener(page().encode())
    with PublicShareClient(opener=opener) as client:
        assert client.get(ID) == conversation()
    request, = opener.requests
    assert request.full_url == f'https://chatgpt.com/share/{ID}'
    assert request.get_method() == 'GET'
    assert {key.lower() for key in request.headers} == {'accept', 'user-agent'}


@pytest.mark.parametrize('identifier', ['../private', 'https://evil.example', OTHER.upper(), None])
def test_invalid_identifiers_do_not_request(identifier):
    opener = Opener(b'')
    with pytest.raises(PublicShareError, match='UUID'):
        PublicShareClient(opener=opener).get(identifier)
    assert opener.requests == []


def test_redirect_rejection():
    assert public._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.example') is None


def test_response_cap(monkeypatch):
    monkeypatch.setattr(public, 'MAX_BYTES', 100)
    with pytest.raises(PublicShareError, match='response limit'):
        PublicShareClient(opener=Opener(b'x' * 101)).get(ID)


def test_redacted_http_errors():
    error = urllib.error.HTTPError('secret-url', 404, 'secret-message', {}, io.BytesIO(b'secret-body'))
    with pytest.raises(PublicShareError, match='HTTP 404') as caught:
        PublicShareClient(opener=Opener(error)).get(ID)
    assert 'secret' not in str(caught.value)


def test_backing_payload_identity_can_differ_from_share_route():
    payload = {**conversation(), "conversation_id": OTHER}
    assert parse_share_html(page(payload), ID)["conversation_id"] == OTHER


def test_unrelated_deferred_updates_do_not_change_published_snapshot():
    html = page() + '<script>' + public._STREAM_CALL + json.dumps('P1:["additional root config"]') + ');</script>'
    assert parse_share_html(html, ID) == conversation()


def test_deferred_conversation_response_is_rejected():
    root = {"loaderData": {"share": {
        "sharedConversationId": ID, "serverResponse": ["Promise", 1],
    }}}
    html = '<script>' + public._STREAM_CALL + json.dumps(json.dumps(table_for(root))) + ');</script>'
    with pytest.raises(PublicShareError):
        parse_share_html(html, ID)


def test_unrelated_tagged_root_values_are_not_decoded():
    root = {"loaderData": {"root": {"tagged": []}, "share": {
        "sharedConversationId": ID,
        "serverResponse": {"type": "data", "data": conversation()},
    }}}
    table = table_for(root)
    table[table.index([])] = ["Set", 99]
    html = '<script>' + public._STREAM_CALL + json.dumps(json.dumps(table)) + ');</script>'
    assert parse_share_html(html, ID) == conversation()


def test_tagged_value_within_conversation_is_rejected():
    payload = {**conversation(), "unsupported": []}
    root = {"loaderData": {"share": {
        "sharedConversationId": ID,
        "serverResponse": {"type": "data", "data": payload},
    }}}
    table = table_for(root)
    table[table.index([])] = ["Set", 99]
    html = '<script>' + public._STREAM_CALL + json.dumps(json.dumps(table)) + ');</script>'
    with pytest.raises(PublicShareError, match='invalid hydration reference'):
        parse_share_html(html, ID)

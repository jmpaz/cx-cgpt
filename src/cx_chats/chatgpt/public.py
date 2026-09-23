from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from html.parser import HTMLParser
from uuid import UUID

from ..http import NoRedirect, TransportError

MAX_BYTES = 32 * 1024 * 1024
MAX_TABLE_ENTRIES = 500_000
MAX_DEPTH = 256
_STREAM_CALL = "window.__reactRouterContext.streamController.enqueue("


class PublicShareError(TransportError):
    pass


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts: list[tuple[str | None, str]] = []
        self._active = False
        self._identifier: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._active = True
            self._identifier = dict(attrs).get("id")
            self._parts = []

    def handle_data(self, data):
        if self._active:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._active:
            self.scripts.append((self._identifier, "".join(self._parts)))
            self._active = False


class _ReferenceTable:
    def __init__(self, table):
        if not isinstance(table, list) or not table or len(table) > MAX_TABLE_ENTRIES:
            raise PublicShareError("Public share contains an unsupported hydration table.")
        self.table = table
        self.memo: dict[int, object] = {}
        self.visiting: set[int] = set()

    def fields(self, reference):
        if type(reference) is not int or reference < 0 or reference >= len(self.table):
            raise PublicShareError("Public share contains an invalid object reference.")
        value = self.table[reference]
        if not isinstance(value, dict):
            raise PublicShareError("Public share contains an unsupported route object.")
        result = {}
        for key, child in value.items():
            if not key.startswith("_") or not key[1:].isdigit():
                raise PublicShareError("Public share contains unsupported hydration object keys.")
            decoded_key = self.resolve(int(key[1:]))
            if not isinstance(decoded_key, str) or decoded_key in result:
                raise PublicShareError("Public share contains invalid hydration object keys.")
            result[decoded_key] = child
        return result

    def resolve(self, reference, depth=0):
        if type(reference) is not int:
            raise PublicShareError("Public share contains an invalid hydration reference.")
        if reference == -5:
            return None
        if reference < 0 or reference >= len(self.table):
            raise PublicShareError("Public share contains an unsupported hydration reference.")
        if depth > MAX_DEPTH or reference in self.visiting:
            raise PublicShareError("Public share hydration is cyclic or nested too deeply.")
        if reference in self.memo:
            return self.memo[reference]
        self.visiting.add(reference)
        value = self.table[reference]
        try:
            if isinstance(value, dict):
                result = {}
                for key, child in value.items():
                    if not key.startswith("_") or not key[1:].isdigit():
                        raise PublicShareError("Public share contains unsupported hydration object keys.")
                    decoded_key = self.resolve(int(key[1:]), depth + 1)
                    if not isinstance(decoded_key, str) or decoded_key in result:
                        raise PublicShareError("Public share contains invalid hydration object keys.")
                    result[decoded_key] = self.resolve(child, depth + 1)
            elif isinstance(value, list):
                result = [self.resolve(child, depth + 1) for child in value]
            else:
                if isinstance(value, float) and not math.isfinite(value):
                    raise PublicShareError("Public share contains a nonfinite value.")
                result = value
            self.memo[reference] = result
            return result
        finally:
            self.visiting.remove(reference)


def _conversation(value, share_id):
    if not isinstance(value, dict):
        raise PublicShareError("Public share contains no conversation data.")
    if value.get("is_public") is False:
        raise PublicShareError("This share is not public.")
    identifier = value.get("conversation_id", value.get("id"))
    if identifier is not None and not isinstance(identifier, str):
        raise PublicShareError("Public share returned an invalid conversation ID.")
    mapping, current = value.get("mapping"), value.get("current_node")
    if not isinstance(mapping, dict) or not mapping or not isinstance(current, str) or current not in mapping:
        raise PublicShareError("Public share contains no complete conversation mapping.")
    return {**value, "conversation_id": identifier or share_id}


def _server_response(value, share_id):
    if not isinstance(value, dict) or value.get("type") != "data":
        raise PublicShareError("Public share is unavailable or requires access.")
    return _conversation(value.get("data"), share_id)


def parse_share_html(html, share_id):
    parser = _Scripts()
    parser.feed(html)
    parser.close()
    decoder = json.JSONDecoder()
    chunks = []
    for identifier, script in parser.scripts:
        if identifier == "__NEXT_DATA__":
            try:
                data = json.loads(script)
                props = data["props"]["pageProps"]
            except (ValueError, TypeError, KeyError):
                raise PublicShareError("Public share contains invalid legacy hydration data.") from None
            if props.get("sharedConversationId", share_id) != share_id:
                raise PublicShareError("Public share page identifies a different share.")
            if "serverResponse" in props:
                return _server_response(props["serverResponse"], share_id)
            for key in ("sharedConversation", "conversation"):
                if key in props:
                    return _conversation(props[key], share_id)
        position = 0
        while True:
            start = script.find(_STREAM_CALL, position)
            if start < 0:
                break
            start += len(_STREAM_CALL)
            try:
                chunk, consumed = decoder.raw_decode(script[start:].lstrip())
            except ValueError:
                raise PublicShareError("Public share contains invalid hydration JSON.") from None
            if not isinstance(chunk, str):
                raise PublicShareError("Public share contains unsupported hydration chunks.")
            chunks.append(chunk)
            position = start + consumed
    if not chunks:
        raise PublicShareError("Public share page contains no supported conversation data.")
    try:
        stream = "".join(chunks).lstrip()
        table, _ = decoder.raw_decode(stream)
        references = _ReferenceTable(table)
        root = references.fields(0)
        loaders = references.fields(root.get("loaderData"))
    except (ValueError, TypeError, RecursionError):
        raise PublicShareError("Public share contains invalid hydration data.") from None
    for route_reference in loaders.values():
        if type(route_reference) is not int or not 0 <= route_reference < len(table):
            continue
        if not isinstance(table[route_reference], dict):
            continue
        route = references.fields(route_reference)
        if "sharedConversationId" in route:
            if references.resolve(route["sharedConversationId"]) != share_id:
                raise PublicShareError("Public share page identifies a different share.")
            response = references.resolve(route.get("serverResponse"))
            return _server_response(response, share_id)
    raise PublicShareError("Public share contains no shared conversation route.")


class PublicShareClient:
    def __init__(self, *, timeout=30, opener=None):
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def get(self, share_id):
        try:
            canonical = str(UUID(share_id))
        except (ValueError, TypeError, AttributeError):
            raise PublicShareError("Public share IDs must be UUIDs.") from None
        if share_id != canonical:
            raise PublicShareError("Public share IDs must be canonical UUIDs.")
        request = urllib.request.Request(
            f"https://chatgpt.com/share/{canonical}",
            headers={"Accept": "text/html", "User-Agent": "cx-chats/0.1.0"},
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise PublicShareError("Public share exceeds the 32 MiB response limit.")
            html = raw.decode("utf-8")
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise PublicShareError(f"Public share request failed (HTTP {status}).") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise PublicShareError("Public share request failed; check network connectivity.") from None
        except UnicodeDecodeError:
            raise PublicShareError("Public share returned invalid text.") from None
        return parse_share_html(html, canonical)

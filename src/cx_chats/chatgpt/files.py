from __future__ import annotations

import posixpath
import re
from typing import Any
from urllib.parse import unquote

from ..files import MAX_FILE_BYTES, ChatFile, decoded, textual, unique_label
from ..http import TransportError
from ..transcript import iso_timestamp
from .render import _active_branch

SANDBOX = "/mnt/data/"
MAX_OUTPUTS = 100
_LINK = re.compile(r"\(sandbox:(/mnt/data/[^()\n]+)\)|sandbox:(/mnt/data/[^\s()\[\]<>\"'`]+)")
_REASONING = {"thoughts", "reasoning_recap"}


def _visible(message: Any, role: str) -> bool:
    if not isinstance(message, dict):
        return False
    author, detail = message.get("author"), message.get("metadata")
    detail = detail if isinstance(detail, dict) else {}
    return (isinstance(author, dict) and author.get("role") == role
            and not detail.get("is_visually_hidden_from_conversation") and not detail.get("is_user_system_message"))


def _reply_parts(message: Any) -> list[str]:
    content = message.get("content") if isinstance(message, dict) else None
    if (not _visible(message, "assistant") or not isinstance(content, dict)
            or message.get("recipient") not in (None, "all") or message.get("channel") == "analysis"
            or content.get("content_type") in _REASONING):
        return []
    parts = content.get("parts")
    return [part for part in parts if isinstance(part, str)] if isinstance(parts, list) else []


def linked_paths(payload: dict[str, Any]) -> dict[str, str]:
    linked: dict[str, str] = {}
    for node in _active_branch(payload)[0]:
        message = node.get("message")
        message_id = message.get("id") if isinstance(message, dict) else None
        if not isinstance(message_id, str):
            continue
        for text in _reply_parts(message):
            for match in _LINK.finditer(text):
                path = posixpath.normpath(unquote((match.group(1) or match.group(2)).strip().rstrip(".,;:!?")))
                if path.startswith(SANDBOX):
                    linked.setdefault(path, message_id)
    return linked


def _failed(error: TransportError, described: dict[str, Any]) -> ChatFile:
    return ChatFile(content=None, omitted=f"download failed: {error}", **described)


def _downloaded(client: Any, url: Any, described: dict[str, Any]) -> ChatFile:
    if described.get("size") is not None and described["size"] > MAX_FILE_BYTES:
        return ChatFile(content=None, omitted="larger than 2 MiB", **described)
    try:
        data = client.download(url, limit=MAX_FILE_BYTES)
    except TransportError as error:
        return _failed(error, described)
    text, omitted = decoded(data)
    return ChatFile(content=text, omitted=omitted, **{**described, "size": described.get("size") or len(data)})


def _whole_number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _output(client: Any, conversation_id: str, message_id: str, path: str, label: str) -> ChatFile:
    described: dict[str, Any] = {"label": label, "origin": "output", "path": path}
    try:
        info = client.sandbox_file(conversation_id, message_id, path)
    except TransportError as error:
        return _failed(error, described)
    content_type = info.get("mime_type")
    described.update(content_type=content_type if isinstance(content_type, str) else None,
                     size=_whole_number(info.get("file_size_bytes")), created=iso_timestamp(info.get("creation_time")))
    if not textual(path, described["content_type"]):
        return ChatFile(content=None, omitted="not text", **described)
    return _downloaded(client, info.get("download_url"), described)


def outputs(client: Any, payload: dict[str, Any], conversation_id: str, *, only: str | None = None) -> list[ChatFile]:
    files: list[ChatFile] = []
    taken: set[str] = set()
    for path, message_id in linked_paths(payload).items():
        label = unique_label("outputs/" + path.removeprefix(SANDBOX), taken)
        if only is not None and label != only:
            continue
        if len(files) >= MAX_OUTPUTS:
            files.append(ChatFile(label, "output", None, path=path, omitted=f"beyond the first {MAX_OUTPUTS} files"))
            continue
        files.append(_output(client, conversation_id, message_id, path, label))
    return files


def uploads(client: Any, payload: dict[str, Any], conversation_id: str, *, only: str | None = None) -> list[ChatFile]:
    files: list[ChatFile] = []
    taken: set[str] = set()
    for node in _active_branch(payload)[0]:
        message = node.get("message")
        if not _visible(message, "user") or not isinstance(message.get("metadata"), dict):
            continue
        for attachment in message["metadata"].get("attachments") or []:
            if not isinstance(attachment, dict) or not isinstance(attachment.get("id"), str):
                continue
            name = str(attachment.get("name") or attachment["id"]).replace("/", "_").strip()
            content_type = attachment.get("mime_type") if isinstance(attachment.get("mime_type"), str) else None
            if not textual(name, content_type):
                continue
            label = unique_label(f"uploads/{name}", taken)
            if only is not None and label != only:
                continue
            described = {"label": label, "origin": "upload", "content_type": content_type,
                         "size": _whole_number(attachment.get("size")),
                         "created": iso_timestamp(message.get("create_time")), "attachment_id": attachment["id"]}
            try:
                info = client.uploaded_file(conversation_id, attachment["id"])
            except TransportError as error:
                files.append(_failed(error, described))
                continue
            files.append(_downloaded(client, info.get("download_url"), described))
    return files

from __future__ import annotations

from typing import Any

from ..files import MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, ChatFile, archived, decoded, textual, unique_label, unpacked
from ..http import TransportError
from ..transcript import iso_timestamp

SANDBOX = "/mnt/user-data/"
MAX_OUTPUTS = 100


def uploads(branch: list[dict[str, Any]]) -> list[ChatFile]:
    files: list[ChatFile] = []
    taken: set[str] = set()
    pasted = 0
    for message in branch:
        if message.get("sender") != "human":
            continue
        for attachment in message.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            name = str(attachment.get("file_name") or "").replace("/", "_").strip()
            if not name:
                pasted += 1
                name = f"pasted-{pasted}.txt"
            text = attachment.get("extracted_content")
            files.append(ChatFile(
                label=unique_label(f"uploads/{name}", taken), origin="upload",
                content=text if isinstance(text, str) else None,
                content_type=attachment.get("file_type"),
                size=attachment.get("file_size") if isinstance(attachment.get("file_size"), int) else None,
                created=iso_timestamp(attachment.get("created_at")),
                omitted=None if isinstance(text, str) else "claude.ai kept no text for it",
                attachment_id=attachment.get("id"),
            ))
    return files


def _output(client: Any, conversation_id: str, path: str, label: str, entry: dict[str, Any]) -> list[ChatFile]:
    size = entry.get("size") if isinstance(entry.get("size"), int) else None
    content_type = entry.get("content_type") if isinstance(entry.get("content_type"), str) else None
    described = {"label": label, "origin": "output", "content_type": content_type, "size": size,
                 "created": iso_timestamp(entry.get("created_at")), "path": path}
    zipped = archived(path, content_type)
    limit = MAX_ARCHIVE_BYTES if zipped else MAX_FILE_BYTES
    if not zipped and not textual(path, content_type):
        return [ChatFile(content=None, omitted="not text", **described)]
    if size is not None and size > limit:
        return [ChatFile(content=None, omitted="a zip larger than 16 MiB" if zipped else "larger than 2 MiB", **described)]
    try:
        data = client.output_file(conversation_id, path, limit=limit)
    except TransportError as error:
        return [ChatFile(content=None, omitted=f"download failed: {error}", **described)]
    if zipped:
        return unpacked(ChatFile(content=None, **described), data)
    text, omitted = decoded(data)
    return [ChatFile(content=text, omitted=omitted, **described)]


def outputs(client: Any, conversation_id: str, *, only: str | None = None) -> list[ChatFile]:
    listed = client.output_files(conversation_id)
    details = {
        entry["path"]: entry for entry in listed.get("files_metadata") or []
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    files: list[ChatFile] = []
    taken: set[str] = set()
    for path in listed.get("files") or []:
        if not isinstance(path, str) or not path.strip():
            continue
        label = unique_label(path.removeprefix(SANDBOX) if path.startswith(SANDBOX) else path.lstrip("/"), taken)
        if only is not None and only != label and not only.startswith(label + "/"):
            continue
        if len(files) >= MAX_OUTPUTS:
            files.append(ChatFile(label, "output", None, path=path, omitted=f"beyond the first {MAX_OUTPUTS} files"))
            continue
        files.extend(_output(client, conversation_id, path, label, details.get(path, {})))
    return [file for file in files if file.label == only] if only is not None else files

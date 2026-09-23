from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from ..http import TransportError
from ..transcript import iso_timestamp

SANDBOX = "/mnt/user-data/"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_OUTPUTS = 100
_TEXT_APPLICATIONS = {
    "application/json", "application/xml", "application/javascript", "application/x-yaml",
    "application/yaml", "application/toml", "application/sql", "application/x-sh",
}
_TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst", ".tex", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".xml", ".html", ".htm", ".css", ".svg", ".js", ".mjs", ".ts", ".tsx",
    ".jsx", ".py", ".rb", ".go", ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".php", ".sh", ".bash", ".zsh", ".fish", ".sql", ".r", ".lua", ".nix", ".mermaid",
}


@dataclass(frozen=True)
class ChatFile:
    label: str
    origin: str
    content: str | None
    content_type: str | None = None
    size: int | None = None
    created: str | None = None
    omitted: str | None = None
    attachment_id: str | None = None
    path: str | None = None

    def described(self) -> str:
        details = [self.content_type]
        if isinstance(self.size, int):
            details.append(f"{self.size:,} bytes")
        return ", ".join(str(detail) for detail in details if detail)


def _unique(label: str, taken: set[str]) -> str:
    candidate, number = label, 1
    path = PurePosixPath(label)
    while candidate in taken:
        number += 1
        candidate = str(path.with_name(f"{path.stem}-{number}{path.suffix}"))
    taken.add(candidate)
    return candidate


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
                label=_unique(f"uploads/{name}", taken), origin="upload",
                content=text if isinstance(text, str) else None,
                content_type=attachment.get("file_type"),
                size=attachment.get("file_size") if isinstance(attachment.get("file_size"), int) else None,
                created=iso_timestamp(attachment.get("created_at")),
                omitted=None if isinstance(text, str) else "claude.ai kept no text for it",
                attachment_id=attachment.get("id"),
            ))
    return files


def _textual(path: str, content_type: str | None) -> bool:
    kind = (content_type or "").split(";")[0].strip().lower()
    return (kind.startswith("text/") or kind in _TEXT_APPLICATIONS
            or PurePosixPath(path).suffix.lower() in _TEXT_SUFFIXES)


def _output(client: Any, conversation_id: str, path: str, label: str, entry: dict[str, Any]) -> ChatFile:
    size = entry.get("size") if isinstance(entry.get("size"), int) else None
    content_type = entry.get("content_type") if isinstance(entry.get("content_type"), str) else None
    described = {"label": label, "origin": "output", "content_type": content_type, "size": size,
                 "created": iso_timestamp(entry.get("created_at")), "path": path}
    if not _textual(path, content_type):
        return ChatFile(content=None, omitted="not text", **described)
    if size is not None and size > MAX_FILE_BYTES:
        return ChatFile(content=None, omitted="larger than 2 MiB", **described)
    try:
        data = client.output_file(conversation_id, path, limit=MAX_FILE_BYTES)
    except TransportError as error:
        return ChatFile(content=None, omitted=f"download failed: {error}", **described)
    if b"\0" in data:
        return ChatFile(content=None, omitted="not text", **described)
    try:
        return ChatFile(content=data.decode("utf-8"), **described)
    except UnicodeDecodeError:
        return ChatFile(content=None, omitted="not UTF-8 text", **described)


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
        label = _unique(path.removeprefix(SANDBOX) if path.startswith(SANDBOX) else path.lstrip("/"), taken)
        if only is not None and label != only:
            continue
        if len(files) >= MAX_OUTPUTS:
            files.append(ChatFile(label, "output", None, path=path, omitted=f"beyond the first {MAX_OUTPUTS} files"))
            continue
        files.append(_output(client, conversation_id, path, label, details.get(path, {})))
    return files

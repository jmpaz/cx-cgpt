from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from .transcript import label

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_FILES = 100
_ARCHIVES = {"application/zip", "application/x-zip-compressed"}
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


def unique_label(candidate: str, taken: set[str]) -> str:
    chosen, number = candidate, 1
    path = PurePosixPath(candidate)
    while chosen in taken:
        number += 1
        chosen = str(path.with_name(f"{path.stem}-{number}{path.suffix}"))
    taken.add(chosen)
    return chosen


def textual(path: str, content_type: str | None) -> bool:
    kind = (content_type or "").split(";")[0].strip().lower()
    return (kind.startswith("text/") or kind in _TEXT_APPLICATIONS
            or PurePosixPath(path).suffix.lower() in _TEXT_SUFFIXES)


def archived(path: str, content_type: str | None) -> bool:
    kind = (content_type or "").split(";")[0].strip().lower()
    return kind in _ARCHIVES or PurePosixPath(path).suffix.lower() == ".zip"


# A zip a model wrote is listed as the archive, and each text file in it follows as a file of its own
# under the archive's label, the way a folder's files do.
def unpacked(archive: ChatFile, data: bytes) -> list[ChatFile]:
    try:
        opened = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return [replace(archive, omitted="not a readable zip")]
    members = [info for info in opened.infolist() if not info.is_dir()]
    files = [replace(archive, size=archive.size or len(data), omitted=f"a zip; its {len(members)} files follow as {archive.label}/…")]
    for number, info in enumerate(members):
        member = ChatFile(f"{archive.label}/{info.filename}", archive.origin, None, created=archive.created,
                          size=info.file_size, path=f"{archive.path}/{info.filename}")
        if number >= MAX_ARCHIVE_FILES:
            files.append(replace(member, omitted=f"beyond the first {MAX_ARCHIVE_FILES} files in the zip"))
        elif not textual(info.filename, None):
            files.append(replace(member, omitted="not text"))
        elif info.file_size > MAX_FILE_BYTES:
            files.append(replace(member, omitted="larger than 2 MiB"))
        else:
            try:
                text, omitted = decoded(opened.read(info))
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
                text, omitted = None, f"not readable from the zip: {error}"
            files.append(replace(member, content=text, omitted=omitted))
    return files


def decoded(data: bytes) -> tuple[str | None, str | None]:
    if b"\0" in data:
        return None, "not text"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "not UTF-8 text"


def file_index(files: list[ChatFile], problem: str | None = None) -> list[str]:
    included = [file for file in files if file.content is not None]
    omitted = [file for file in files if file.content is None]
    lines = []
    if included:
        lines.extend(["Files following the transcript:", *[f"- {label(file.label)}" for file in included], ""])
    if omitted:
        lines.extend(["Files not included:", *[
            f"- {label(file.label)}" + (f" ({file.described()})" if file.described() else "") + f": {label(file.omitted)}"
            for file in omitted
        ], ""])
    if problem:
        lines.extend([f"Files could not be listed: {label(problem)}", ""])
    return lines


def file_summary(file: ChatFile) -> dict:
    return {"label": file.label, "origin": file.origin, "content_type": file.content_type, "size": file.size,
            "created": file.created, "included": file.content is not None, "omitted": file.omitted}


def file_document(file: ChatFile, *, provider: str, source: str, conversation_id: str, source_url: str) -> dict:
    return {
        "source": source, "label": file.label, "content": file.content, "prose": "", "prose_authors": [],
        "metadata": {
            "provider": provider, "kind": "file", "conversation_id": conversation_id,
            "origin": file.origin, "path": file.path, "content_type": file.content_type, "size": file.size,
            "source_created": file.created, "attachment_id": file.attachment_id,
            "source_url": source_url, "source_ref": provider, "scope_id": conversation_id, "trace_path": source,
            "context_subpath": f"{provider}/{conversation_id}/{file.label}",
        },
    }

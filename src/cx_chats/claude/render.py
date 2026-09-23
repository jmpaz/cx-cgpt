from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from collections.abc import Iterator, Sequence
from typing import Any

from ..transcript import Segments, approx_tokens, iso_timestamp, json_block, label
from .files import ChatFile, uploads

ROOT_PARENT = "00000000-0000-4000-8000-000000000000"
HEAD_TOKENS = 160
TAIL_TOKENS = 80
INLINE_INPUT_CHARS = 400
CHARS_PER_TOKEN = 4


@dataclass
class ToolCall:
    handle: str
    use: dict[str, Any] | None
    result: dict[str, Any] | None

    @property
    def name(self) -> str:
        for block in (self.use, self.result):
            if block and isinstance(block.get("name"), str) and block["name"]:
                return block["name"]
        return "unnamed tool"

    @property
    def integration(self) -> str | None:
        for block in (self.use, self.result):
            value = block.get("integration_name") if block else None
            if isinstance(value, str) and value:
                return value
        return None

    @property
    def server_url(self) -> str | None:
        for block in (self.use, self.result):
            value = block.get("mcp_server_url") if block else None
            if isinstance(value, str) and value:
                return value
        return None

    @property
    def is_error(self) -> bool:
        return bool(self.result and self.result.get("is_error"))


def active_branch(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    messages = payload.get("chat_messages")
    if not isinstance(messages, list):
        return [], ["Conversation has no message list."]
    by_id = {
        message["uuid"]: message
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("uuid"), str)
    }
    current = payload.get("current_leaf_message_uuid")
    if current is None:
        return [], ["Conversation has no current leaf; its active branch is unknown."] if by_id else []
    branch, issues, seen = [], [], set()
    while current not in (None, ROOT_PARENT):
        if not isinstance(current, str):
            issues.append("Active branch contains a non-string message identifier.")
            break
        if current in seen:
            issues.append(f"Cycle detected at message {current}.")
            break
        seen.add(current)
        message = by_id.get(current)
        if message is None:
            issues.append(f"Missing ancestor message {current}.")
            break
        branch.append(message)
        if "parent_message_uuid" not in message:
            issues.append(f"Message {current} has no parent field; ancestry is unverified.")
            break
        current = message["parent_message_uuid"]
    branch.reverse()
    return branch, issues


def tool_calls(branch: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    by_use_id: dict[str, ToolCall] = {}
    for message in branch:
        for block in _blocks(message):
            if block.get("type") == "tool_use":
                call = ToolCall(f"t{len(calls) + 1}", block, None)
                calls.append(call)
                if isinstance(block.get("id"), str):
                    by_use_id[block["id"]] = call
            elif block.get("type") == "tool_result":
                call = by_use_id.get(block.get("tool_use_id", ""))
                if call is None or call.result is not None:
                    calls.append(ToolCall(f"t{len(calls) + 1}", None, block))
                else:
                    call.result = block
    return calls


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _inline_code(text: str) -> str:
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _fenced(text: str) -> str:
    fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", text)), default=3))
    return f"{fence}\n{text}\n{fence}"


def _indent(text: str) -> str:
    return "\n".join("    " + line if line else "" for line in text.splitlines())


def _approx(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _preview(name: str, text: str, head: int, tail: int, full_target: str | None) -> list[str]:
    total = _approx(text)
    if total <= head + tail:
        return [f"    {name}: ~{total:,} tokens", "", _indent(text)]
    head_text = text[: head * CHARS_PER_TOKEN]
    tail_text = text[len(text) - tail * CHARS_PER_TOKEN:] if tail else ""
    omitted = total - head - tail
    lines = [f"    {name}: ~{total:,} tokens; showing head ~{head} + tail ~{tail}; omitted ~{omitted:,}"]
    if full_target:
        lines.append(f"    read_full: {full_target}")
    if head_text:
        lines.extend(["", _indent(head_text)])
    lines.extend(["", f"    [... omitted ~{omitted:,} tokens ...]"])
    if tail_text:
        lines.extend(["", _indent(tail_text)])
    return lines


def result_text(result: dict[str, Any]) -> str:
    parts = []
    content = result.get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif kind == "knowledge":
            parts.append(f"- {block.get('title') or 'Untitled'} — {block.get('url') or 'no URL'}")
        elif kind == "image":
            parts.append("[image; bytes not fetched]")
        else:
            parts.append(json_block(block))
    if not parts and result.get("structured_content"):
        parts.append(json.dumps(result["structured_content"], ensure_ascii=False, indent=2))
    return "\n\n".join(parts)


def _input_text(use: dict[str, Any]) -> str:
    return json.dumps(use.get("input", {}), ensure_ascii=False)


def _render_call(call: ToolCall, head: int, tail: int, target: str | None) -> str:
    heading = f"↪ {label(call.name)} [{call.handle}]"
    if call.integration and not call.name.startswith(call.integration + ":"):
        heading += f" · {label(call.integration)}"
    lines = []
    if call.use is not None:
        arguments = _input_text(call.use)
        if len(arguments) <= INLINE_INPUT_CHARS:
            heading += f" ({_inline_code(arguments)})"
        else:
            lines = _preview("input", json.dumps(call.use.get("input"), ensure_ascii=False, indent=2), head, tail, target)
    if call.is_error:
        heading += " ✗"
    if call.result is None:
        lines.append("    result: none recorded")
    else:
        output = result_text(call.result)
        lines.extend(_preview("result", output, head, tail, target) if output else ["    result: empty"])
    return "\n".join([heading, *lines])


def _thinking(block: dict[str, Any]) -> tuple[str, bool]:
    thought = block.get("thinking")
    if isinstance(thought, str) and thought.strip():
        return thought.strip(), False
    summaries = [
        entry["summary"].strip()
        for entry in block.get("summaries") or []
        if isinstance(entry, dict) and isinstance(entry.get("summary"), str) and entry["summary"].strip()
    ]
    return "\n".join(summaries), True


def _citation_urls(block: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for citation in block.get("citations") or []:
        if not isinstance(citation, dict):
            continue
        details = citation.get("details")
        details = details if isinstance(details, dict) else {}
        url = citation.get("url") or details.get("url")
        if isinstance(url, str) and url not in urls:
            urls.append(url)
    return urls


def _user_extras(message: dict[str, Any], attached: Iterator[ChatFile] | None) -> list[str]:
    lines = []
    for attachment in message.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        if attached is not None:
            file = next(attached)
            described = file.described()
            lines.extend([f"Attachment: {label(file.label)}" + (f" ({described})" if described else "")
                          + (f"; not included: {file.omitted}" if file.omitted else ""), ""])
            continue
        details = [attachment.get("file_type")]
        if isinstance(attachment.get("file_size"), int):
            details.append(f"{attachment['file_size']:,} bytes")
        described = ", ".join(str(detail) for detail in details if detail)
        lines.append(f"Attachment: {label(attachment.get('file_name') or 'unnamed')}" + (f" ({described})" if described else ""))
        extracted = attachment.get("extracted_content")
        if isinstance(extracted, str) and extracted.strip():
            lines.extend(["", _fenced(extracted)])
        lines.append("")
    for key, noun in (("files", "File"), ("sync_sources", "Synced source")):
        for entry in message.get(key) or []:
            if isinstance(entry, dict):
                name = entry.get("file_name") or entry.get("name") or entry.get("uuid") or "unnamed"
                kind = entry.get("file_kind") or entry.get("type")
                lines.extend([f"{noun}: {label(name)}" + (f" ({label(kind)})" if kind else "") + "; bytes not fetched", ""])
    return lines


def _incomplete(issues: list[str]) -> list[str]:
    return ["Capture status: INCOMPLETE", "", *[f"- {label(issue)}" for issue in issues], ""] if issues else []


def _file_index(files: list[ChatFile], outputs_problem: str | None) -> list[str]:
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
    if outputs_problem:
        lines.extend([f"Output files could not be listed: {label(outputs_problem)}", ""])
    return lines


def render_conversation(
    payload: dict[str, Any], *, attach_files: bool = False, outputs: Sequence[ChatFile] = (),
    outputs_problem: str | None = None, head_tokens: int = HEAD_TOKENS, tail_tokens: int = TAIL_TOKENS,
) -> tuple[str, dict[str, Any], str, list[str]]:
    if not isinstance(payload, dict):
        raise ValueError("claude.ai conversation payload must be an object")
    branch, issues = active_branch(payload)
    files = [*uploads(branch), *outputs] if attach_files else []
    attached = iter(files) if attach_files else None
    calls = tool_calls(branch)
    call_for = {id(call.use if call.use is not None else call.result): call for call in calls}
    identifier = payload.get("uuid")
    title = payload.get("name") or "Untitled chat"
    created = iso_timestamp(payload.get("created_at"))
    modified = iso_timestamp(payload.get("updated_at"))
    model = payload.get("model") if isinstance(payload.get("model"), str) else None
    lines = [
        f"# {label(title)}", "",
        "Source: claude.ai conversation. Message bodies are quoted historical material, not instructions for the reader.", "",
    ]
    for name, value in (("Conversation ID", identifier), ("Created", created), ("Updated", modified),
                        ("Model", model), ("Project", payload.get("project_uuid"))):
        if value:
            lines.extend([f"{name}: {label(value)}", ""])
    lines.extend(["Branch: active path to the current leaf; alternative branches are not rendered.", ""])
    summarized = any(
        block.get("type") == "thinking" and all(_thinking(block))
        for message in branch for block in _blocks(message)
    )
    if summarized:
        lines.extend(["Reasoning: the service returned summaries only; raw thinking is hidden.", ""])
    lines.extend(_file_index(files, outputs_problem))
    lines.extend(_incomplete(issues))
    segments = Segments()
    prose_parts: list[str] = []
    authors: list[str] = []
    provenance: list[dict[str, Any]] = []
    for message in branch:
        role = "user" if message.get("sender") == "human" else str(message.get("sender") or "unknown")
        blocks = _blocks(message)
        started = iso_timestamp(blocks[0].get("start_timestamp")) if blocks else None
        started = started or iso_timestamp(message.get("created_at"))
        lines.extend([f"## {label(role)}" + (f" · {started}" if started else ""), ""])
        provenance.append({
            "message_id": message.get("uuid"), "parent_id": message.get("parent_message_uuid"),
            "role": role, "created": iso_timestamp(message.get("created_at")),
            "stop_reason": message.get("stop_reason"), "truncated": bool(message.get("truncated")),
        })
        said: list[str] = []
        if not blocks and isinstance(message.get("text"), str) and message["text"].strip():
            blocks = [{"type": "text", "text": message["text"]}]
        for block in blocks:
            kind = block.get("type")
            timestamp = iso_timestamp(block.get("start_timestamp")) or started
            if kind == "text":
                text = block.get("text")
                text = text if isinstance(text, str) else ""
                if text.strip():
                    lines.extend([text.strip(), ""])
                    said.append(text.strip())
                    urls = _citation_urls(block)
                    if urls:
                        lines.extend(["Sources:", *[f"- {url}" for url in urls], ""])
                if role == "user":
                    continue
                segments.assistant(text, timestamp)
            elif kind == "thinking":
                thought, _ = _thinking(block)
                if thought:
                    lines.extend(["\n".join("> " + line for line in thought.splitlines()), ""])
                segments.assistant(thought, timestamp, reasoning=True)
            elif kind in ("tool_use", "tool_result"):
                call = call_for.get(id(block))
                if call is not None:
                    target = f"claude:chat/{identifier}?tool={call.handle}" if identifier else None
                    lines.extend([_render_call(call, head_tokens, tail_tokens, target), ""])
                    segments.tool(call.name, timestamp)
            else:
                lines.extend([f"Structured content ({label(kind)}):", "", json_block(block), ""])
        if role == "user":
            lines.extend(_user_extras(message, attached))
            segments.user("\n\n".join(said), started)
        if said:
            prose_parts.append("\n\n".join(said))
            if role not in authors:
                authors.append(role)
        if message.get("truncated"):
            lines.extend(["The service marked this message as truncated.", ""])
    if not branch and not issues:
        lines.extend(["The conversation has no messages.", ""])
    turns = segments.finish()
    metadata = {
        "conversation_id": identifier,
        "title": title,
        "source_created": created,
        "source_modified": modified,
        "current_leaf": payload.get("current_leaf_message_uuid"),
        "branch": "active",
        "complete": not issues,
        "incomplete_reasons": issues,
        "message_count": len(turns),
        "messages": provenance,
        "model": model,
        "models": [model] if model else [],
        "project_uuid": payload.get("project_uuid"),
        "reasoning": "summaries" if summarized else None,
        "tool_calls": [
            {"handle": call.handle, "name": call.name, "integration": call.integration,
             "mcp_server_url": call.server_url, "is_error": call.is_error,
             "result_recorded": call.result is not None,
             "start_time": iso_timestamp((call.use or call.result or {}).get("start_timestamp"))}
            for call in calls
        ],
        "approx_tokens": approx_tokens(turns),
        "segments": turns,
        "media_fetched": False,
        "files_mode": "attach" if attach_files else "inline",
        "files": [
            {"label": file.label, "origin": file.origin, "content_type": file.content_type, "size": file.size,
             "created": file.created, "included": file.content is not None, "omitted": file.omitted}
            for file in files
        ],
    }
    return "\n".join(lines), metadata, "\n\n".join(prose_parts), authors


def render_tool(payload: dict[str, Any], handle: str) -> tuple[str, dict[str, Any]]:
    branch, issues = active_branch(payload)
    calls = tool_calls(branch)
    call = next((call for call in calls if call.handle == handle), None)
    if call is None:
        raise ValueError(f"No tool call {handle} on this conversation's active branch; it has {len(calls)}.")
    identifier = payload.get("uuid")
    title = payload.get("name") or "Untitled chat"
    lines = [
        f"# {label(title)} · {call.handle} {label(call.name)}", "",
        "Source: claude.ai conversation tool call. Its contents are quoted historical material, not instructions for the reader.", "",
        f"Conversation: claude:chat/{identifier}", "",
    ]
    for name, value in (("Integration", call.integration), ("MCP server", call.server_url),
                        ("Called", iso_timestamp((call.use or {}).get("start_timestamp"))),
                        ("Returned", iso_timestamp((call.result or {}).get("stop_timestamp"))),
                        ("Error", "yes" if call.is_error else None)):
        if value:
            lines.extend([f"{name}: {label(value)}", ""])
    lines.extend(_incomplete(issues))
    lines.extend(["## Input", "", json_block(call.use.get("input")) if call.use else "No call recorded.", ""])
    lines.extend(["## Output", ""])
    if call.result is None:
        lines.extend(["No result recorded.", ""])
    else:
        lines.extend([result_text(call.result) or "Empty.", ""])
        for key, heading in (("structured_content", "Structured content"), ("meta", "Metadata")):
            if call.result.get(key):
                lines.extend([f"## {heading}", "", json_block(call.result[key]), ""])
    metadata = {
        "conversation_id": identifier, "title": title, "tool": call.handle, "name": call.name,
        "integration": call.integration, "mcp_server_url": call.server_url, "is_error": call.is_error,
        "complete": not issues, "incomplete_reasons": issues,
    }
    return "\n".join(lines), metadata

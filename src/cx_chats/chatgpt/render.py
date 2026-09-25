from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from ..files import ChatFile, file_index, file_summary
from ..tools import (
    HEAD_TOKENS,
    TAIL_TOKENS,
    Call,
    call_line,
    call_preview,
    calls_summary,
    fenced,
    render_calls,
    select,
    tools_note,
)
from ..query import with_query
from ..transcript import RENDER_VERSION, Segments, approx_tokens, iso_timestamp, json_block, label
from ..variants import Message, Tree, header_lines, note_lines, render_map

SANDBOX_LINK = "sandbox:/mnt/data/"
NOT_KEPT = "no output kept by ChatGPT"
_MARKER = re.compile("\ue200[^\ue201]*\ue201")
_WRITING = re.compile(r"^:::writing\{(?P<attributes>[^}\n]*)\}\n(?P<body>.*?)\n:::[ \t]*$", re.M | re.S)
_ATTRIBUTE = re.compile(r'(\w+)="([^"]*)"')
_PLAIN_CONTENT = {"content_type", "text", "parts", "language", "response_format_name"}


def _active_branch(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    mapping = payload.get("mapping")
    if not isinstance(mapping, dict):
        return [], ["Conversation has no message mapping."]
    current = payload.get("current_node")
    if current is None:
        return [], ["Conversation has no current_node; its active branch is unknown."]
    nodes, issues, seen = [], [], set()
    while current is not None:
        if not isinstance(current, str):
            issues.append("Active branch contains a non-string node identifier.")
            break
        if current in seen:
            issues.append(f"Cycle detected at node {current}.")
            break
        seen.add(current)
        node = mapping.get(current)
        if not isinstance(node, dict):
            issues.append(f"Missing ancestor node {current}.")
            break
        nodes.append(node)
        if "parent" not in node:
            issues.append(
                f"Node {current} has no parent field; ancestry is unverified."
            )
            break
        current = node["parent"]
    nodes.reverse()
    return nodes, issues


def _detail(message: dict[str, Any]) -> dict[str, Any]:
    detail = message.get("metadata")
    return detail if isinstance(detail, dict) else {}


def _author(message: dict[str, Any]) -> tuple[str, str | None]:
    author = message.get("author")
    author = author if isinstance(author, dict) else {}
    name = author.get("name")
    return str(author.get("role") or "unknown"), name if isinstance(name, str) and name else None


def _content(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if isinstance(content, dict):
        return content
    return {"content_type": "text", "parts": [content]} if isinstance(content, str) else {}


def _reasoning(message: dict[str, Any]) -> bool:
    return _content(message).get("content_type") in ("thoughts", "reasoning_recap") or message.get("channel") == "analysis"


def _quoted(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def _hidden(message: dict[str, Any]) -> bool:
    detail = _detail(message)
    return (
        _author(message)[0] == "system"
        or bool(detail.get("is_visually_hidden_from_conversation"))
        or bool(detail.get("is_user_system_message"))
    )


def _text(content: dict[str, Any]) -> str:
    parts = content.get("parts")
    if isinstance(parts, list):
        rendered = []
        for part in parts:
            if isinstance(part, str):
                rendered.append(part)
            elif isinstance(part, dict) and part.get("content_type") == "image_asset_pointer":
                pointer = part.get("asset_pointer")
                rendered.append(f"[image {label(pointer)}; not fetched]" if pointer else "[image; not fetched]")
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                rendered.append(part["text"])
            else:
                rendered.append("Structured content part:\n\n" + json_block(part))
        return "\n\n".join(part for part in rendered if part)
    for key in ("text", "content"):
        if isinstance(content.get(key), str):
            return content[key]
    return ""


def _transcribed(part: Any) -> bool:
    return isinstance(part, dict) and part.get("content_type") == "audio_transcription" and isinstance(part.get("text"), str)


# Voice mode carries what was said, on either side, as transcription parts rather than text.
def _prose(content: dict[str, Any]) -> str:
    parts = content.get("parts")
    if isinstance(parts, list):
        said = (part if isinstance(part, str) else part["text"] for part in parts if isinstance(part, str) or _transcribed(part))
        return "\n\n".join(text for text in said if text)
    return content["text"] if isinstance(content.get("text"), str) else ""


def _voice(message: dict[str, Any]) -> bool:
    parts = _content(message).get("parts")
    return isinstance(parts, list) and any(map(_transcribed, parts))


def _referenced(text: str, detail: dict[str, Any]) -> str:
    references = [
        reference for reference in detail.get("content_references") or []
        if isinstance(reference, dict) and isinstance(reference.get("matched_text"), str) and reference["matched_text"]
    ]
    for reference in sorted(references, key=lambda item: item.get("start_idx") or 0, reverse=True):
        matched = reference["matched_text"]
        replacement = reference["alt"] if isinstance(reference.get("alt"), str) else ""
        start, end = reference.get("start_idx"), reference.get("end_idx")
        if isinstance(start, int) and isinstance(end, int) and text[start:end] == matched:
            text = text[:start] + replacement + text[end:]
        elif "\ue200" in matched:
            text = text.replace(matched, replacement, 1)
    return _MARKER.sub("", text)


def _writing_block(match: re.Match[str]) -> str:
    attributes = dict(_ATTRIBUTE.findall(match.group("attributes")))
    name = attributes.get("title") or attributes.get("subject") or attributes.get("id") or "untitled"
    variant = attributes.get("variant")
    kind = f"Writing block ({variant})" if variant and variant != "document" else "Writing block"
    return f"{kind}: {label(name)}\n\n" + fenced(match.group("body"), "markdown")


def _reply(message: dict[str, Any], attach_files: bool) -> str:
    text = _WRITING.sub(_writing_block, _referenced(_text(_content(message)), _detail(message)))
    return text.replace(SANDBOX_LINK, "outputs/").replace("sandbox:/", "outputs/") if attach_files else text


def _thoughts(content: dict[str, Any]) -> str:
    if content.get("content_type") == "reasoning_recap":
        return content["content"].strip() if isinstance(content.get("content"), str) else ""
    lines = []
    for thought in content.get("thoughts") if isinstance(content.get("thoughts"), list) else []:
        if not isinstance(thought, dict):
            continue
        for key in ("summary", "content"):
            value = thought.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(value.strip())
    return "\n\n".join(lines)


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _call(message: dict[str, Any], handle: str) -> Call:
    recipient = str(message.get("recipient"))
    raw = _text(_content(message))
    parsed = _json(raw)
    name, integration, value = recipient, None, parsed if isinstance(parsed, (dict, list)) else raw
    if recipient == "api_tool.call_tool" and isinstance(parsed, dict) and isinstance(parsed.get("path"), str):
        pieces = [piece for piece in parsed["path"].split("/") if piece]
        if len(pieces) > 1:
            integration, name = pieces[0], f"{pieces[0]}:{pieces[-1]}"
        elif pieces:
            name = pieces[0]
        value = parsed.get("args")
    return Call(handle, name, integration, input=None if value in ("", None) else value,
                called=iso_timestamp(message.get("create_time")))


def _answers(name: str | None, recipient: str) -> bool:
    return bool(name) and (name == recipient or recipient.startswith(f"{name}.") or name.startswith(f"{recipient}."))


def tool_calls(messages: list[dict[str, Any]]) -> tuple[list[Call], dict[str, Call]]:
    calls: list[Call] = []
    by_message: dict[str, Call] = {}
    pending: list[tuple[str, Call]] = []
    for message in messages:
        role, name = _author(message)
        identifier = str(message.get("id"))
        recipient = message.get("recipient")
        if role == "assistant" and recipient not in (None, "all"):
            call = _call(message, f"t{len(calls) + 1}")
            calls.append(call)
            pending.append((str(recipient), call))
            by_message[identifier] = call
            continue
        if role != "tool":
            continue
        content = _content(message)
        output = _text(content)
        call = next((call for called, call in reversed(pending) if call.output is None and _answers(name, called)), None)
        if call is None:
            if not output.strip():
                continue
            call = Call(f"t{len(calls) + 1}", name or "tool")
            calls.append(call)
        call.output = output if output.strip() else None
        call.output_note = None if output.strip() else NOT_KEPT
        call.returned = iso_timestamp(message.get("create_time"))
        call.is_error = call.is_error or content.get("content_type") == "system_error"
        extra = {key: value for key, value in content.items() if key not in _PLAIN_CONTENT and value is not None}
        call.structured = extra or call.structured
        by_message[identifier] = call
    return calls, by_message


def _mapping(payload: dict[str, Any]) -> dict[str, Any]:
    mapping = payload.get("mapping")
    return mapping if isinstance(mapping, dict) else {}


def variant_tree(payload: dict[str, Any]) -> Tree:
    messages = []
    for key, node in _mapping(payload).items():
        if not isinstance(node, dict):
            continue
        parent = node.get("parent") if isinstance(node.get("parent"), str) else None
        message = node.get("message")
        if not isinstance(message, dict):
            messages.append(Message(key, parent, "assistant", None, meaningful=False))
            continue
        role, _ = _author(message)
        content = _content(message)
        side = "user" if role == "user" else "assistant"
        replying = role == "assistant" and message.get("recipient") in (None, "all") and not _reasoning(message)
        said = _prose(content) if role == "user" else _referenced(_prose(content), _detail(message)) if replying else ""
        messages.append(Message(
            key, parent, side, iso_timestamp(message.get("create_time")), said.strip(),
            meaningful=not _hidden(message) and not (role == "tool" and not _text(content).strip()),
            model=_model(message) if side == "assistant" else None,
            qualifier="dictated" if role == "user" and _detail(message).get("dictation") is True else None,
        ))
    return Tree(messages, payload.get("current_node"))


def variant_payload(payload: dict[str, Any], variant: str | None) -> dict[str, Any]:
    if not variant:
        return payload
    return {**payload, "current_node": variant_tree(payload).leaf_for(variant)}


def render_conversation_map(payload: dict[str, Any], target: str) -> tuple[str, dict[str, Any]]:
    tree = variant_tree(payload)
    title = payload.get("title") or "Untitled conversation"
    return render_map(tree, title, target), {
        "conversation_id": payload.get("conversation_id") or payload.get("id"), "title": title,
        "variants": tree.summary(),
    }


_LATE_SAVE_SECONDS = 60


# Voice mode sometimes saves a message long after it was said, stamping it with the time it was
# saved. A message stamped more than a minute after the person's next message cannot have come
# before it, so it takes the latest trustworthy time ahead of it and is marked as inferred. Shorter
# overlaps are real: in voice either side can speak over the other.
def _dated(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    next_user: list[float | None] = [None] * len(messages)
    upcoming = None
    for index in range(len(messages) - 1, -1, -1):
        next_user[index] = upcoming
        created = messages[index].get("create_time")
        if _author(messages[index])[0] == "user" and isinstance(created, (int, float)):
            upcoming = created
    dated, floor = [], None
    for message, ceiling in zip(messages, next_user):
        created = message.get("create_time")
        if isinstance(created, (int, float)) and ceiling is not None and created > ceiling + _LATE_SAVE_SECONDS and floor is not None:
            dated.append({**message, "create_time": floor, "_time_inferred": True})
            continue
        if isinstance(created, (int, float)):
            floor = created if floor is None else max(floor, created)
        dated.append(message)
    return dated


def _turns(messages: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    turns: list[tuple[str, list[dict[str, Any]]]] = []
    for message in messages:
        role = "user" if _author(message)[0] == "user" else "assistant"
        if role == "user" or not turns or turns[-1][0] == "user":
            turns.append((role, [message]))
        else:
            turns[-1][1].append(message)
    return turns


def _model(message: dict[str, Any]) -> str | None:
    model = _detail(message).get("model_slug")
    return model if isinstance(model, str) and model else None


def _heading(role: str, messages: list[dict[str, Any]]) -> str:
    qualifiers = []
    if role == "user" and _detail(messages[0]).get("dictation") is True:
        qualifiers.append("dictated")
    elif role == "user" and _voice(messages[0]):
        qualifiers.append("voice")
    models = [model for model in map(_model, messages) if model]
    if role == "assistant" and models:
        qualifiers.append(label(models[-1]))
    if messages[0].get("_time_inferred"):
        qualifiers.append("time inferred")
    started = iso_timestamp(messages[0].get("create_time"))
    return "## " + " · ".join([role, *qualifiers, *([started] if started else [])])


def _attachments(message: dict[str, Any], attached: dict[str, ChatFile]) -> list[str]:
    lines = []
    for attachment in _detail(message).get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        file = attached.get(str(attachment.get("id")))
        if file is not None:
            described = file.described()
            lines.extend([f"Attachment: {label(file.label)}" + (f" ({described})" if described else ""), ""])
            continue
        details = [attachment.get("mime_type")]
        if isinstance(attachment.get("size"), int):
            details.append(f"{attachment['size']:,} bytes")
        described = ", ".join(str(detail) for detail in details if detail)
        lines.extend([f"Attachment: {label(attachment.get('name') or attachment.get('id') or 'unnamed')}"
                      + (f" ({described})" if described else "") + "; not fetched", ""])
    return lines


def _provenance(message: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    role, name = _author(message)
    detail = _detail(message)
    return {
        "message_id": message.get("id") or node.get("id"),
        "node_id": node.get("id"),
        "role": role,
        "name": name,
        "model": _model(message),
        "created": iso_timestamp(message.get("create_time")),
        "updated": iso_timestamp(message.get("update_time")),
        "recipient": message.get("recipient"),
        "channel": message.get("channel"),
        "content_type": _content(message).get("content_type"),
        "dictated": detail.get("dictation") if isinstance(detail.get("dictation"), bool) else None,
    }


def _assistant_lines(message: dict[str, Any], call: Call | None, tools: str, head: int, tail: int,
                     target: str | None, attach_files: bool) -> list[str]:
    if call is not None:
        if tools == "none":
            return []
        full = with_query(target, f"tool={call.handle}") if target else None
        return [call_line(call) if tools == "lines" else call_preview(call, head, tail, full), ""]
    content = _content(message)
    kind = content.get("content_type")
    if _reasoning(message):
        thought = (_thoughts(content) if kind in ("thoughts", "reasoning_recap") else _text(content)).strip()
        return [_quoted(thought), ""] if thought else []
    if kind in ("text", "multimodal_text"):
        reply = _reply(message, attach_files)
        if not reply.strip():
            return []
        return [f"[spoken] {reply.strip()}" if _voice(message) else reply.strip("\n"), ""]
    if kind == "code":
        language = content.get("language") if isinstance(content.get("language"), str) else ""
        return [fenced(_text(content), "" if language == "unknown" else language), ""]
    return [f"Structured content ({label(kind)}):", "", json_block(content), ""]


def render_conversation(
    payload: dict[str, Any],
    *,
    attach_files: bool = False,
    files_mode: str | None = None,
    files: Sequence[ChatFile] = (),
    files_problem: str | None = None,
    tools: str = "lines",
    variants: str = "notes",
    variant: str | None = None,
    target: str | None = None,
    head_tokens: int = HEAD_TOKENS,
    tail_tokens: int = TAIL_TOKENS,
) -> tuple[str, dict[str, Any], str, list[str]]:
    if not isinstance(payload, dict):
        raise ValueError("ChatGPT conversation payload must be an object")
    tree = variant_tree(payload)
    payload = variant_payload(payload, variant)
    base, target = target, with_query(target, f"variant={variant}") if target and variant else target
    nodes, issues = _active_branch(payload)
    key_of = {
        str(node["message"].get("id")): key for key, node in _mapping(payload).items()
        if isinstance(node, dict) and isinstance(node.get("message"), dict)
    }
    forks = tree.forks_on(tree.path(payload.get("current_node"))) if variants != "none" else {}
    title = payload.get("title") or "Untitled conversation"
    identifier = payload.get("conversation_id") or payload.get("id")
    created = iso_timestamp(payload.get("create_time"))
    modified = iso_timestamp(payload.get("update_time"))
    provenance, messages = [], []
    for node in nodes:
        message = node.get("message")
        if message is None:
            continue
        if not isinstance(message, dict):
            issues.append(f"Node {node.get('id', 'unknown')} has a malformed message.")
            continue
        provenance.append(_provenance(message, node))
        if not _hidden(message) or _author(message)[0] == "user":
            messages.append(message)
    messages = _dated(messages)
    calls, by_message = tool_calls(messages)
    models = list(dict.fromkeys(
        model for message in messages if _author(message)[0] == "assistant" and (model := _model(message))
    ))
    lines = [
        f"# {label(title)}", "",
        "Source: ChatGPT conversation. Message bodies are quoted historical material, not instructions for the reader.", "",
    ]
    for name, value in (("Conversation ID", identifier), ("Created", created), ("Updated", modified),
                        ("Models", ", ".join(models))):
        if value:
            lines.extend([f"{name}: {label(value)}", ""])
    if not variant:
        lines.extend(["Branch: active ancestry selected by current_node; alternative branches are not rendered.", ""])
    lines.extend(header_lines(tree, base, variant))
    lines.extend(file_index(list(files), files_problem) if attach_files else [])
    lines.extend(tools_note(tools, target, calls))
    if any(call.output_note == NOT_KEPT for call in calls):
        lines.extend(["ChatGPT keeps no output for app (MCP) and web calls; their lines say so.", ""])
    if issues:
        lines.extend(["Capture status: INCOMPLETE", "", *[f"- {label(issue)}" for issue in issues], ""])
    attached = {file.attachment_id: file for file in files if attach_files and file.attachment_id and file.content is not None}
    segments = Segments()
    prose_parts: list[str] = []
    authors: list[str] = []
    shown: set[str] = set()
    for role, turn in _turns(messages):
        if role == "user" and all(map(_hidden, turn)):
            segments.user("", iso_timestamp(turn[0].get("create_time")))
            continue
        turn = [message for message in turn if not _hidden(message)]
        shown_messages = turn if role == "user" else [
            message for message in turn if _author(message)[0] == "assistant" or str(message.get("id")) in by_message
        ]
        lines.extend([_heading(role, shown_messages or turn), ""])
        for message in turn:
            key = key_of.get(str(message.get("id")))
            if key in forks:
                lines.extend(note_lines(forks[key], key, variants))
        if role == "user":
            said = "\n\n".join(_prose(_content(message)) for message in turn).strip()
            body = "\n\n".join(_text(_content(message)) for message in turn).strip()
            lines.extend([body, ""] if body else [])
            attachment_lines = [line for message in turn for line in _attachments(message, attached)]
            lines.extend(attachment_lines)
            segments.user(said or "\n".join(filter(None, attachment_lines)), iso_timestamp(turn[0].get("create_time")),
                          dictated=_detail(turn[0]).get("dictation") is True, voice=_voice(turn[0]))
            if said:
                prose_parts.append(said)
                if "user" not in authors:
                    authors.append("user")
            continue
        turn_calls: list[Call] = []
        summary_at = None
        for message in turn:
            timestamp = iso_timestamp(message.get("create_time"))
            model = _model(message)
            call = by_message.get(str(message.get("id")))
            if call is not None:
                if call.handle in shown:
                    continue
                shown.add(call.handle)
                turn_calls.append(call)
                if summary_at is None:
                    summary_at = len(lines)
                lines.extend(_assistant_lines(message, call, tools, head_tokens, tail_tokens, target, attach_files))
                segments.tool(call.name, timestamp, model=model)
                continue
            if _author(message)[0] != "assistant":
                continue
            lines.extend(_assistant_lines(message, None, tools, head_tokens, tail_tokens, target, attach_files))
            content = _content(message)
            if _reasoning(message):
                thought = _thoughts(content) if content.get("content_type") in ("thoughts", "reasoning_recap") else _prose(content)
                segments.assistant(thought, timestamp, reasoning=True, model=model)
                continue
            said = _referenced(_prose(content), _detail(message))
            segments.assistant(f"[spoken] {said.strip()}" if said.strip() and _voice(message) else said, timestamp, model=model)
            if said.strip():
                prose_parts.append(said)
                if "assistant" not in authors:
                    authors.append("assistant")
            lines.extend(_attachments(message, attached))
        if tools == "none" and summary_at is not None:
            lines[summary_at:summary_at] = [calls_summary(turn_calls), ""]
    if not any(not _hidden(message) for message in messages) and not issues:
        lines.extend(["The conversation has no messages.", ""])
    turns = segments.finish()
    metadata = {
        "conversation_id": identifier,
        "title": title,
        "source_created": created,
        "source_modified": modified,
        "current_node": payload.get("current_node"),
        "branch": "active",
        "complete": not issues,
        "incomplete_reasons": issues,
        "message_count": len(turns),
        "render_version": RENDER_VERSION,
        "messages": provenance,
        "model": models[-1] if models else None,
        "models": models,
        "tool_calls": [
            {"handle": call.handle, "name": call.name, "integration": call.integration,
             "output_kept": call.output is not None, "start_time": call.called}
            for call in calls
        ],
        "approx_tokens": approx_tokens(turns),
        "segments": turns,
        "media_fetched": False,
        "files_mode": files_mode or ("attach" if attach_files else "inline"),
        "files": [file_summary(file) for file in files],
        "variant": variant,
        "variants": tree.summary(),
    }
    return "\n".join(lines), metadata, "\n\n".join(prose_parts), authors


def render_tools(payload: dict[str, Any], spec: str, *, target: str, offset: int = 0,
                 tokens: int | None = None, variant: str | None = None) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("ChatGPT conversation payload must be an object")
    nodes, issues = _active_branch(variant_payload(payload, variant))
    messages = [
        node["message"] for node in nodes
        if isinstance(node.get("message"), dict) and not _hidden(node["message"])
    ]
    calls, _ = tool_calls(messages)
    chosen = select(calls, spec)
    title = payload.get("title") or "Untitled conversation"
    target = with_query(target, f"variant={variant}") if variant else target
    lines = [render_calls(title, "ChatGPT conversation", target, chosen, offset=offset, tokens=tokens)]
    if issues:
        lines.extend(["Capture status: INCOMPLETE", "", *[f"- {label(issue)}" for issue in issues], ""])
    metadata = {
        "conversation_id": payload.get("conversation_id") or payload.get("id"), "title": title, "tool": spec,
        "names": [call.name for call in chosen], "complete": not issues, "incomplete_reasons": issues,
    }
    return "\n".join(lines), metadata

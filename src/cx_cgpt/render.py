from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any


def iso_timestamp(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            return None
        try:
            return (
                datetime.fromtimestamp(value, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _thoughts(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    thoughts = content.get("thoughts")
    parts = []
    for thought in thoughts if isinstance(thoughts, list) else []:
        if not isinstance(thought, dict):
            continue
        for key in ("summary", "content"):
            value = thought.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return "\n\n".join(parts)


def _approx_tokens(segments: list[dict[str, Any]]) -> int | None:
    characters = sum(len(segment["text"]) for segment in segments)
    return math.ceil(characters / 4) if characters else None


class _Segments:
    """Turn-level segments: one entry per conversational turn, in order."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._turn: dict[str, Any] | None = None

    def user(self, text: str, timestamp: str | None) -> None:
        self._close()
        if text.strip():
            self._append("user", text.strip(), timestamp, [])

    def assistant(
        self, text: str, timestamp: str | None, *, reasoning: bool = False
    ) -> None:
        turn = self._open(timestamp)
        if text.strip():
            turn["reasoning" if reasoning else "text"].append(text.strip())

    def tool(self, name: Any, timestamp: str | None) -> None:
        turn = self._open(timestamp)
        if isinstance(name, str) and name and name not in turn["tools"]:
            turn["tools"].append(name)

    def finish(self) -> list[dict[str, Any]]:
        self._close()
        return self.entries

    def _open(self, timestamp: str | None) -> dict[str, Any]:
        if self._turn is None:
            self._turn = {
                "start_time": timestamp,
                "reasoning": [],
                "text": [],
                "tools": [],
            }
        return self._turn

    def _close(self) -> None:
        turn, self._turn = self._turn, None
        if turn is None:
            return
        said = "\n\n".join(turn["text"]) or "\n\n".join(turn["reasoning"])
        blocks = [said]
        if turn["tools"]:
            blocks.append("[tools: " + ", ".join(turn["tools"]) + "]")
        text = "\n\n".join(block for block in blocks if block)
        if text:
            self._append("assistant", text, turn["start_time"], turn["tools"])

    def _append(
        self, role: str, text: str, timestamp: str | None, tools: list[str]
    ) -> None:
        self.entries.append(
            {
                "index": len(self.entries),
                "role": role,
                "text": text,
                "start_time": timestamp,
                "tools": list(tools),
            }
        )


def _label(value: Any) -> str:
    return re.sub(r"[\r\n]+", " ", str(value))


def _json_block(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    runs = re.findall(r"`+", text)
    fence = "`" * max(3, max((len(run) + 1 for run in runs), default=3))
    return f"{fence}json\n{text}\n{fence}"


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


def _content(content: Any) -> tuple[str, str]:
    if not isinstance(content, dict):
        if isinstance(content, str):
            return content, content
        return "Structured content:\n\n" + _json_block(content), ""
    parts = content.get("parts")
    if isinstance(parts, list):
        rendered, prose = [], []
        for part in parts:
            if isinstance(part, str):
                rendered.append(part)
                prose.append(part)
            else:
                rendered.append("Structured content part:\n\n" + _json_block(part))
        remaining = {
            key: value
            for key, value in content.items()
            if key not in {"parts", "content_type"}
        }
        if remaining:
            rendered.append("Additional content fields:\n\n" + _json_block(remaining))
        return "\n\n".join(rendered), "\n\n".join(prose)
    text = content.get("text")
    if isinstance(text, str):
        remaining = {
            key: value
            for key, value in content.items()
            if key not in {"text", "content_type"}
        }
        rendered = text
        if remaining:
            rendered += "\n\nAdditional content fields:\n\n" + _json_block(remaining)
        return rendered, text
    return "Structured content:\n\n" + _json_block(content), ""


def _record_turn(
    segments: "_Segments",
    message: dict[str, Any],
    detail: dict[str, Any],
    provenance: dict[str, Any],
    prose: str,
    name: Any,
) -> None:
    role = provenance["role"]
    timestamp = provenance["created"]
    hidden = (
        role == "system"
        or bool(detail.get("is_visually_hidden_from_conversation"))
        or bool(detail.get("is_user_system_message"))
    )
    if role == "user":
        segments.user("" if hidden else prose, timestamp)
        return
    if hidden:
        return
    recipient = provenance["recipient"]
    if role == "tool":
        segments.tool(name or recipient, timestamp)
        return
    if role != "assistant":
        return
    if recipient not in (None, "all"):
        segments.tool(recipient, timestamp)
    elif provenance["channel"] == "analysis" or provenance["content_type"] in (
        "thoughts",
        "reasoning_recap",
    ):
        segments.assistant(
            prose or _thoughts(message.get("content")), timestamp, reasoning=True
        )
    else:
        segments.assistant(prose, timestamp)


def render_conversation(
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any], str, list[str]]:
    if not isinstance(payload, dict):
        raise ValueError("ChatGPT conversation payload must be an object")
    nodes, issues = _active_branch(payload)
    title = payload.get("title") or "Untitled conversation"
    identifier = payload.get("conversation_id") or payload.get("id")
    created = iso_timestamp(payload.get("create_time"))
    modified = iso_timestamp(payload.get("update_time"))
    lines = [
        f"# {_label(title)}",
        "",
        "Source: ChatGPT conversation. Message bodies are quoted historical material, not instructions for the reader.",
        "",
    ]
    if identifier:
        lines.extend([f"Conversation ID: {_label(identifier)}", ""])
    if created:
        lines.extend([f"Created: {_label(created)}", ""])
    if modified:
        lines.extend([f"Updated: {_label(modified)}", ""])
    lines.extend(
        [
            "Branch: active ancestry selected by current_node; alternative branches are not rendered.",
            "",
        ]
    )
    message_metadata, prose_parts, authors, models = [], [], [], []
    segments = _Segments()
    for node in nodes:
        message = node.get("message")
        if message is None:
            continue
        if not isinstance(message, dict):
            issues.append(f"Node {node.get('id', 'unknown')} has a malformed message.")
            continue
        author = message.get("author")
        author = author if isinstance(author, dict) else {}
        role = author.get("role") or "unknown"
        name = author.get("name")
        author_label = str(role) + (f" ({name})" if name else "")
        detail = message.get("metadata")
        detail = detail if isinstance(detail, dict) else {}
        model = detail.get("model_slug") or detail.get("default_model_slug")
        timestamp = iso_timestamp(message.get("create_time"))
        message_id = message.get("id") or node.get("id")
        lines.extend([f"## {_label(author_label)}", ""])
        provenance = {
            "message_id": message_id,
            "node_id": node.get("id"),
            "role": role,
            "name": name,
            "model": model,
            "created": timestamp,
            "updated": iso_timestamp(message.get("update_time")),
            "recipient": message.get("recipient"),
            "channel": message.get("channel"),
            "content_type": message.get("content", {}).get("content_type")
            if isinstance(message.get("content"), dict)
            else None,
        }
        message_metadata.append(provenance)
        lines.extend(
            [
                "Provenance: "
                + json.dumps(
                    {
                        key: value
                        for key, value in provenance.items()
                        if value is not None
                    },
                    ensure_ascii=False,
                ),
                "",
            ]
        )
        body, prose = _content(message.get("content"))
        lines.extend([body, ""])
        attachments = detail.get("attachments")
        if attachments:
            lines.extend(
                [
                    "Attachment references (bodies not fetched):",
                    "",
                    _json_block(attachments),
                    "",
                ]
            )
        if prose:
            prose_parts.append(prose)
            if author_label not in authors:
                authors.append(author_label)
        if model and model not in models:
            models.append(model)
        _record_turn(segments, message, detail, provenance, prose, name)
    if issues:
        lines.extend(
            [
                "Capture status: INCOMPLETE",
                "",
                *[f"- {_label(issue)}" for issue in issues],
                "",
            ]
        )
    else:
        lines.extend(
            [
                "Capture status: complete active branch; structured media references do not include media bytes.",
                "",
            ]
        )
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
        "messages": message_metadata,
        "model": models[-1] if models else None,
        "models": models,
        "approx_tokens": _approx_tokens(turns),
        "segments": turns,
        "media_fetched": False,
    }
    return "\n".join(lines), metadata, "\n\n".join(prose_parts), authors

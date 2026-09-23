from __future__ import annotations

import hashlib
import json
from typing import Any

from ..http import TransportError
from ..transcript import iso_timestamp
from .client import ClaudeClient
from ..files import ChatFile, file_document
from .files import outputs, uploads
from ..tools import HEAD_TOKENS, TAIL_TOKENS, TOOL_MODES
from ..variants import VARIANT_MODES
from .render import active_branch, render_chat_map, render_conversation, render_tools, variant_payload
from .service import read_page, read_search
from .targets import Target, is_claude_target, normalize_options, parse_target

PLUGIN_API_VERSION = "1"
PLUGIN_NAME = "claude"
PLUGIN_PRIORITY = 100
PLUGIN_KIND = "source"

_CLI_OPTIONS = (
    "query", "project", "after", "before", "limit", "offset", "output", "tool", "tools", "file", "files",
    "result_head_tokens", "result_tail_tokens", "result_offset", "result_tokens", "variant", "variants", "map",
)


def can_resolve(target: str, context: dict[str, Any]) -> bool:
    return is_claude_target(target)


def normalize_manifest_config(raw_config: dict | None) -> dict | None:
    return None if raw_config is None else normalize_options(raw_config)


def _parse(raw: str, context: dict) -> Target:
    target = parse_target(raw, context.get("overrides", {}).get("claude"))
    if target is None:
        raise ValueError("Expected a claude.ai target")
    return target


def _require_live(context: dict) -> None:
    if context.get("cache_only"):
        raise ValueError("The claude source reads live history; use a hydrated capture for offline reads")


def classify_target(target: str, context: dict) -> dict | None:
    if not can_resolve(target, context):
        return None
    parsed = _parse(target, context)
    return {
        "provider": PLUGIN_NAME, "kind": parsed.kind, "is_external": True,
        "group_key": "claude", "metadata": {"id": parsed.conversation_id},
        "capabilities": {"resolve": True, "listTargets": True},
    }


def _entry(item: dict) -> dict:
    return {
        "target": f"claude:chat/{item['uuid']}", "label": item.get("name") or "Untitled chat",
        "kind": "chat", "traverse": False,
        "metadata": {
            **item, "conversation_id": item["uuid"], "title": item.get("name"),
            "source_created": iso_timestamp(item.get("created_at")),
            "source_modified": iso_timestamp(item.get("updated_at")),
        },
    }


def _read(client: ClaudeClient, target: Target) -> dict:
    return read_search(client, target) if target.kind == "search" else read_page(client, target)


def _listing(target: Target, page: dict) -> str:
    heading = f"claude.ai search: {target.options['query']}" if target.kind == "search" else "claude.ai chats"
    lines = [f"# {heading.replace(chr(10), ' ')}", ""]
    for item in page["items"]:
        entry = _entry(item)
        lines.append(f"- {str(entry['label']).replace(chr(10), ' ')} — {entry['target']}")
        snippet = item.get("snippet")
        if isinstance(snippet, str) and snippet.strip():
            lines.append("  " + " ".join(snippet.split()))
    lines.extend(["", f"Returned {page['returned']} chats; scanned {page['scanned']} entries."])
    if page["next_target"]:
        lines.extend(["", f"Continue: {page['next_target']}"])
    if page["scan_limit_reached"]:
        lines.extend(["", "Scan limit reached; more history remains."])
    if page.get("server_limit_reached"):
        lines.extend(["", "claude.ai returns at most 200 matches; narrow the query or scope it to a project for others."])
    if "after" in target.options or "before" in target.options:
        lines.extend(["", "Dates filter updated_at: after is inclusive; before is exclusive; dates use UTC."])
    return "\n".join(lines) + "\n"


def list_targets(target: str, context: dict) -> dict:
    parsed = _parse(target, context)
    if parsed.kind == "chat":
        return {"targets": [], "summary": {"kind": "chat", "hint": "Resolve this target to read its active branch."}, "pagination": None}
    _require_live(context)
    paging = {
        key: context[name] for name, key in (("list_limit", "limit"), ("list_offset", "offset"))
        if context.get(name) is not None and not (key == "offset" and "offset" in parsed.options)
    }
    parsed = Target(parsed.kind, None, normalize_options({**parsed.options, **paging}, parsed.kind))
    with ClaudeClient() as client:
        page = _read(client, parsed)
    return {
        "targets": [_entry(item) for item in page["items"]],
        "summary": {key: value for key, value in page.items() if key != "items"},
        "pagination": {
            "offset": context.get("list_offset", page["offset"]),
            "limit": context.get("list_limit", page["limit"]),
            "returned": page["returned"], "hasMore": page["next_target"] is not None,
            "nextOffset": page["next_offset"], "nextTarget": page["next_target"],
        },
        "metadata": {"provider": PLUGIN_NAME, "next_target": page["next_target"]},
        "capabilities": {"resolve": True, "listTargets": True},
    }


def _file_document(conversation_id: str, file: ChatFile) -> dict:
    return file_document(
        file, provider=PLUGIN_NAME, source=Target("chat", conversation_id, {"file": file.label}).canonical,
        conversation_id=conversation_id, source_url=f"https://claude.ai/chat/{conversation_id}",
    )


def _chat_file(client: ClaudeClient, payload: dict, conversation_id: str, wanted: str) -> ChatFile:
    branch, _ = active_branch(payload)
    found = [file for file in uploads(branch) if file.label == wanted]
    if not found and not wanted.startswith("uploads/"):
        found = outputs(client, conversation_id, only=wanted)
    if not found:
        raise ValueError(f"No file {wanted} in this chat; the transcript's header lists its files.")
    if found[0].content is None:
        raise ValueError(f"{wanted} is not included: {found[0].omitted}.")
    return found[0]


def _outputs(client: ClaudeClient, conversation_id: str) -> tuple[list[ChatFile], str | None]:
    try:
        return outputs(client, conversation_id), None
    except TransportError as error:
        return [], str(error)


def _read_chat(client: ClaudeClient, parsed: Target) -> tuple[list[dict], dict]:
    options, identifier = parsed.options, parsed.conversation_id
    payload = client.conversation(identifier)
    returned = payload.get("uuid")
    if returned is not None and returned != identifier:
        raise ValueError("claude.ai returned a different conversation ID")
    view = variant_payload(payload, options.get("variant"))
    if "file" in options:
        return [_file_document(identifier, _chat_file(client, view, identifier, options["file"]))], payload
    files: list[ChatFile] = []
    key = identifier + "".join(f"/{options[name]}" for name in ("variant", "tool") if name in options)
    if "map" in options:
        content, metadata = render_chat_map(payload)
        prose, authors = "", []
        key = f"{identifier}/map"
    elif "tool" in options:
        content, metadata = render_tools(payload, options["tool"], offset=options.get("result_offset", 0),
                                         tokens=options.get("result_tokens"), variant=options.get("variant"))
        prose, authors = "", []
    else:
        attach = options.get("files", "attach") == "attach"
        found, problem = _outputs(client, identifier) if attach else ([], None)
        content, metadata, prose, authors = render_conversation(
            payload, attach_files=attach, outputs=found, outputs_problem=problem,
            tools=options.get("tools", "lines"), variants=options.get("variants", "notes"),
            variant=options.get("variant"),
            head_tokens=options.get("result_head_tokens", HEAD_TOKENS),
            tail_tokens=options.get("result_tail_tokens", TAIL_TOKENS),
        )
        files = [*uploads(active_branch(view)[0]), *found] if attach else []
    metadata["source_url"] = f"https://claude.ai/chat/{identifier}"
    transcript = {
        "source": parsed.canonical, "label": metadata.get("title") or parsed.canonical,
        "content": content, "prose": prose, "prose_authors": authors,
        "metadata": {
            **metadata, "provider": PLUGIN_NAME, "kind": parsed.kind,
            "source_ref": "claude", "scope_id": identifier,
            "trace_path": parsed.canonical, "context_subpath": f"claude/{key}.md",
        },
    }
    return [transcript, *[_file_document(identifier, file) for file in files if file.content is not None]], payload


def _listing_document(parsed: Target, page: dict) -> dict:
    key = hashlib.sha256(parsed.canonical.encode()).hexdigest()[:24]
    return {
        "source": parsed.canonical, "label": parsed.canonical,
        "content": _listing(parsed, page), "prose": "", "prose_authors": [],
        "metadata": {
            **{name: value for name, value in page.items() if name != "items"},
            "provider": PLUGIN_NAME, "kind": parsed.kind, "source_ref": "claude", "scope_id": parsed.kind,
            "trace_path": parsed.canonical, "context_subpath": f"claude/{key}.md",
        },
    }


def resolve(target: str, context: dict) -> list[dict]:
    parsed = _parse(target, context)
    _require_live(context)
    with ClaudeClient() as client:
        if parsed.kind == "chat":
            documents, payload = _read_chat(client, parsed)
        else:
            payload = _read(client, parsed)
            documents = [_listing_document(parsed, payload)]
    if parsed.options.get("output") == "json" and "file" not in parsed.options:
        documents[0]["content"] = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return documents


def register_cli_options(command_name: str, command: Any) -> None:
    if command_name not in {"cat", "hydrate", "payload"}:
        return
    import click

    existing = {opt for param in command.params for opt in getattr(param, "opts", ())}
    for name in _CLI_OPTIONS:
        flag = "--claude-" + name.replace("_", "-")
        if flag in existing:
            continue
        if name == "map":
            command.params.append(click.Option([flag, "claude_map"], is_flag=True, default=False,
                                               help="Outline every version of a claude.ai chat."))
            continue
        option_type = (
            click.Choice(["transcript", "json"]) if name == "output"
            else click.Choice(["attach", "inline"]) if name == "files"
            else click.Choice(list(TOOL_MODES)) if name == "tools"
            else click.Choice(list(VARIANT_MODES)) if name == "variants"
            else int if name in {"limit", "offset", "result_head_tokens", "result_tail_tokens",
                                 "result_offset", "result_tokens"} else str
        )
        command.params.append(click.Option([flag, "claude_" + name], type=option_type, default=None,
                                           help=f"claude.ai {name.replace('_', ' ')}; see cx-chats README."))


def collect_cli_overrides(command_name: str, params: dict) -> dict | None:
    if command_name not in {"cat", "hydrate", "payload"}:
        return None
    values = {key: params.get(f"claude_{key}") for key in _CLI_OPTIONS}
    return {key: value for key, value in values.items() if value is not None and value is not False} or None

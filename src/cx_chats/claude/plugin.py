from __future__ import annotations

import hashlib
import json
from typing import Any

from ..transcript import iso_timestamp
from .client import ClaudeClient
from .render import HEAD_TOKENS, TAIL_TOKENS, render_conversation, render_tool
from .service import read_page, read_search
from .targets import Target, is_claude_target, normalize_options, parse_target

PLUGIN_API_VERSION = "1"
PLUGIN_NAME = "claude"
PLUGIN_PRIORITY = 100
PLUGIN_KIND = "source"

_CLI_OPTIONS = (
    "query", "project", "after", "before", "limit", "offset", "output", "tool",
    "result_head_tokens", "result_tail_tokens",
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


def resolve(target: str, context: dict) -> list[dict]:
    parsed = _parse(target, context)
    _require_live(context)
    options = parsed.options
    with ClaudeClient() as client:
        if parsed.kind == "chat":
            payload = client.conversation(parsed.conversation_id)
            returned = payload.get("uuid")
            if returned is not None and returned != parsed.conversation_id:
                raise ValueError("claude.ai returned a different conversation ID")
            if "tool" in options:
                content, metadata = render_tool(payload, options["tool"])
                prose, authors = "", []
                key = f"{parsed.conversation_id}/{options['tool']}"
            else:
                content, metadata, prose, authors = render_conversation(
                    payload,
                    head_tokens=options.get("result_head_tokens", HEAD_TOKENS),
                    tail_tokens=options.get("result_tail_tokens", TAIL_TOKENS),
                )
                key = parsed.conversation_id
            metadata["source_url"] = f"https://claude.ai/chat/{parsed.conversation_id}"
        else:
            payload = _read(client, parsed)
            content = _listing(parsed, payload)
            metadata = {key: value for key, value in payload.items() if key != "items"}
            prose, authors = "", []
            key = hashlib.sha256(parsed.canonical.encode()).hexdigest()[:24]
        if options.get("output") == "json":
            content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return [{
        "source": parsed.canonical, "label": metadata.get("title") or parsed.canonical,
        "content": content, "prose": prose, "prose_authors": authors,
        "metadata": {
            **metadata, "provider": PLUGIN_NAME, "kind": parsed.kind,
            "source_ref": "claude", "scope_id": parsed.conversation_id or parsed.kind,
            "trace_path": parsed.canonical, "context_subpath": f"claude/{key}.md",
        },
    }]


def register_cli_options(command_name: str, command: Any) -> None:
    if command_name not in {"cat", "hydrate", "payload"}:
        return
    import click

    existing = {opt for param in command.params for opt in getattr(param, "opts", ())}
    for name in _CLI_OPTIONS:
        flag = "--claude-" + name.replace("_", "-")
        if flag in existing:
            continue
        option_type = click.Choice(["transcript", "json"]) if name == "output" else (
            int if name in {"limit", "offset", "result_head_tokens", "result_tail_tokens"} else str
        )
        command.params.append(click.Option([flag, "claude_" + name], type=option_type, default=None,
                                           help=f"claude.ai {name.replace('_', ' ')}; see cx-chats README."))


def collect_cli_overrides(command_name: str, params: dict) -> dict | None:
    if command_name not in {"cat", "hydrate", "payload"}:
        return None
    return {
        key: params[f"claude_{key}"] for key in _CLI_OPTIONS if params.get(f"claude_{key}") is not None
    } or None

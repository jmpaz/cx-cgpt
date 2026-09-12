from __future__ import annotations

import hashlib
import json
from typing import Any

from .render import render_conversation
from .service import read_page
from .targets import Target, is_chatgpt_target, normalize_options, parse_target
from .transport import ChatGPTClient

PLUGIN_API_VERSION = "1"
PLUGIN_NAME = "chatgpt"
PLUGIN_PRIORITY = 100
PLUGIN_KIND = "source"


def can_resolve(target: str, context: dict[str, Any]) -> bool:
    return is_chatgpt_target(target)


def normalize_manifest_config(raw_config: dict | None) -> dict | None:
    return None if raw_config is None else normalize_options(raw_config)


def _parse(raw: str, context: dict) -> Target:
    target = parse_target(raw, context.get("overrides", {}).get("chatgpt"))
    if target is None:
        raise ValueError("Expected a ChatGPT target")
    return target


def _require_live(context: dict) -> None:
    if context.get("cache_only"):
        raise ValueError("cx-cgpt reads live history; use a hydrated capture for offline reads")


def classify_target(target: str, context: dict) -> dict | None:
    if not can_resolve(target, context):
        return None
    parsed = _parse(target, context)
    return {
        "provider": PLUGIN_NAME, "kind": parsed.kind, "is_external": True,
        "group_key": "chatgpt", "metadata": {"id": parsed.conversation_id},
        "capabilities": {"resolve": True, "listTargets": True},
    }


def _entry(item: dict) -> dict:
    identifier = item.get("conversation_id", item.get("id"))
    return {
        "target": f"chatgpt:thread/{identifier}", "label": item.get("title") or "Untitled conversation",
        "kind": "thread", "traverse": False, "metadata": item,
    }


def _envelope(page: dict, context: dict) -> dict:
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


def list_targets(target: str, context: dict) -> dict:
    parsed = _parse(target, context)
    if parsed.kind == "thread":
        return {"targets": [], "summary": {"kind": "thread", "hint": "Resolve this target to read its active branch."}, "pagination": None}
    _require_live(context)
    overrides = {
        key: context[name] for name, key in (("list_limit", "limit"), ("list_offset", "offset"))
        if context.get(name) is not None
    }
    parsed = Target(parsed.kind, None, normalize_options({**parsed.options, **overrides}, parsed.kind))
    with ChatGPTClient() as client:
        page = read_page(client, parsed)
    return _envelope(page, context)


def resolve(target: str, context: dict) -> list[dict]:
    parsed = _parse(target, context)
    _require_live(context)
    with ChatGPTClient() as client:
        if parsed.kind == "thread":
            payload = client.get(f"/conversation/{parsed.conversation_id}")
            returned_id = payload.get("conversation_id", payload.get("id"))
            if returned_id is not None and returned_id != parsed.conversation_id:
                raise ValueError("ChatGPT returned a different conversation ID")
            payload = {**payload, "conversation_id": parsed.conversation_id}
            content, metadata, prose, authors = render_conversation(payload)
            key = parsed.conversation_id
        else:
            payload = read_page(client, parsed)
            entries = [_entry(item) for item in payload["items"]]
            content = "# ChatGPT history\n\n" + "\n".join(
                f"- {str(entry['label']).replace(chr(10), ' ')} — {entry['target']}"
                for entry in entries
            )
            content += f"\n\nReturned {len(entries)} conversations; scanned {payload['scanned']} entries.\n"
            if payload["next_target"]:
                content += f"\nContinue: {payload['next_target']}\n"
            if payload["scan_limit_reached"]:
                content += "\nScan limit reached; more history remains.\n"
            if "after" in parsed.options or "before" in parsed.options:
                content += "\nDates filter update_time: after is inclusive; before is exclusive; dates use UTC.\n"
            metadata = {key: value for key, value in payload.items() if key != "items"}
            prose, authors = "", []
            key = hashlib.sha256(parsed.canonical.encode()).hexdigest()[:24]
        if parsed.options.get("output") == "json":
            content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        return [{
            "source": parsed.canonical, "label": metadata.get("title") or parsed.canonical,
            "content": content, "prose": prose, "prose_authors": authors,
            "metadata": {
                **metadata, "provider": PLUGIN_NAME, "kind": parsed.kind,
                "source_ref": "chatgpt", "scope_id": parsed.conversation_id or parsed.kind,
                "trace_path": parsed.canonical, "context_subpath": f"chatgpt/{key}.md",
            },
        }]


def register_cli_options(command_name: str, command: Any) -> None:
    if command_name not in {"cat", "hydrate", "payload"}:
        return
    import click

    existing = {opt for param in command.params for opt in getattr(param, "opts", ())}
    for name in ("query", "after", "before", "limit", "offset", "cursor", "output"):
        flag = f"--chatgpt-{name}"
        if flag in existing:
            continue
        option_type = click.Choice(["transcript", "json"]) if name == "output" else (
            int if name in {"limit", "offset"} else str
        )
        command.params.append(click.Option([flag], type=option_type, default=None, help=f"ChatGPT {name}; see cx-cgpt README."))


def collect_cli_overrides(command_name: str, params: dict) -> dict | None:
    if command_name not in {"cat", "hydrate", "payload"}:
        return None
    return {
        key: params[f"chatgpt_{key}"] for key in ("query", "after", "before", "limit", "offset", "cursor", "output")
        if params.get(f"chatgpt_{key}") is not None
    } or None

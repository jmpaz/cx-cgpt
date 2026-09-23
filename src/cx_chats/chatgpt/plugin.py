from __future__ import annotations

import hashlib
import json
from typing import Any

from ..files import ChatFile, file_document
from ..tools import HEAD_TOKENS, TAIL_TOKENS, TOOL_MODES
from ..transcript import iso_timestamp
from .files import outputs, uploads
from .public import PublicShareClient
from ..variants import VARIANT_MODES
from .render import render_conversation, render_conversation_map, render_tools, variant_payload
from .service import read_page
from .targets import Target, is_chatgpt_target, normalize_options, parse_target
from .transport import ChatGPTClient

PLUGIN_API_VERSION = "1"
PLUGIN_NAME = "chatgpt"
PLUGIN_PRIORITY = 100
PLUGIN_KIND = "source"

_CLI_OPTIONS = (
    "query", "after", "before", "limit", "offset", "cursor", "output", "file", "files", "tool", "tools",
    "result_head_tokens", "result_tail_tokens", "result_offset", "result_tokens", "variant", "variants", "map",
)
_INTEGER_OPTIONS = {"limit", "offset", "result_head_tokens", "result_tail_tokens", "result_offset", "result_tokens"}


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
        raise ValueError("The chatgpt source reads live history; use a hydrated capture for offline reads")


def classify_target(target: str, context: dict) -> dict | None:
    if not can_resolve(target, context):
        return None
    parsed = _parse(target, context)
    return {
        "provider": PLUGIN_NAME, "kind": parsed.kind, "is_external": True,
        "group_key": "chatgpt", "metadata": {"id": parsed.share_id or parsed.conversation_id},
        "capabilities": {"resolve": True, "listTargets": True},
    }


def _entry(item: dict) -> dict:
    identifier = item.get("conversation_id", item.get("id"))
    return {
        "target": f"chatgpt:thread/{identifier}", "label": item.get("title") or "Untitled conversation",
        "kind": "thread", "traverse": False,
        "metadata": {
            **item, "conversation_id": identifier, "title": item.get("title"),
            "source_created": iso_timestamp(item.get("create_time")),
            "source_modified": iso_timestamp(item.get("update_time")),
        },
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
    if parsed.kind in {"thread", "share"}:
        return {"targets": [], "summary": {"kind": parsed.kind, "hint": "Resolve this target to read its active branch."}, "pagination": None}
    _require_live(context)
    paging = {
        key: context[name] for name, key in (("list_limit", "limit"), ("list_offset", "offset"))
        if context.get(name) is not None and not (key == "offset" and "offset" in parsed.options)
    }
    parsed = Target(parsed.kind, None, normalize_options({**parsed.options, **paging}, parsed.kind))
    with ChatGPTClient() as client:
        page = read_page(client, parsed)
    return _envelope(page, context)


def _file_document(conversation_id: str, file: ChatFile) -> dict:
    return file_document(
        file, provider=PLUGIN_NAME, source=Target("thread", conversation_id, {"file": file.label}).canonical,
        conversation_id=conversation_id, source_url=f"https://chatgpt.com/c/{conversation_id}",
    )


def _conversation_file(client: ChatGPTClient, payload: dict, conversation_id: str, wanted: str) -> ChatFile:
    found = (uploads if wanted.startswith("uploads/") else outputs)(client, payload, conversation_id, only=wanted)
    if not found:
        raise ValueError(f"No file {wanted} in this conversation; the transcript's header lists its files.")
    if found[0].content is None:
        raise ValueError(f"{wanted} is not included: {found[0].omitted}.")
    return found[0]


def _render(payload: dict, parsed: Target, base: str, **files: Any) -> tuple[str, dict, str, list[str], str]:
    options = parsed.options
    if "map" in options:
        content, metadata = render_conversation_map(payload, base)
        return content, metadata, "", [], "/map"
    part = "".join(f"/{options[name]}" for name in ("variant", "tool") if name in options)
    if "tool" in options:
        content, metadata = render_tools(payload, options["tool"], target=base, variant=options.get("variant"),
                                         offset=options.get("result_offset", 0), tokens=options.get("result_tokens"))
        return content, metadata, "", [], part
    content, metadata, prose, authors = render_conversation(
        payload, tools=options.get("tools", "lines"), target=base,
        variants=options.get("variants", "notes"), variant=options.get("variant"),
        head_tokens=options.get("result_head_tokens", HEAD_TOKENS),
        tail_tokens=options.get("result_tail_tokens", TAIL_TOKENS), **files,
    )
    return content, metadata, prose, authors, part


def resolve(target: str, context: dict) -> list[dict]:
    parsed = _parse(target, context)
    _require_live(context)
    client_type = PublicShareClient if parsed.kind == "share" else ChatGPTClient
    files: list[ChatFile] = []
    with client_type() as client:
        if parsed.kind == "share":
            payload = client.get(parsed.share_id)
            base = Target("share", None, {}, parsed.share_id).canonical
            content, metadata, prose, authors, part = _render(payload, parsed, base)
            content = "Published share snapshot; completeness refers only to this snapshot, not the private conversation.\n\n" + content
            metadata.update({
                "share_id": parsed.share_id, "authentication": "none",
                "source_url": f"https://chatgpt.com/share/{parsed.share_id}",
                "capture_scope": "published_share_snapshot",
                "backing_conversation_id": payload.get("backing_conversation_id"),
            })
            key = f"shares/{parsed.share_id}{part}"
        elif parsed.kind == "thread":
            payload = client.get(f"/conversation/{parsed.conversation_id}")
            returned_id = payload.get("conversation_id", payload.get("id"))
            if returned_id is not None and returned_id != parsed.conversation_id:
                raise ValueError("ChatGPT returned a different conversation ID")
            payload = {**payload, "conversation_id": parsed.conversation_id}
            view = variant_payload(payload, parsed.options.get("variant"))
            if "file" in parsed.options:
                wanted = _conversation_file(client, view, parsed.conversation_id, parsed.options["file"])
                return [_file_document(parsed.conversation_id, wanted)]
            attach = (parsed.options.get("files", "attach") == "attach"
                      and not parsed.options.keys() & {"tool", "map"})
            files = [
                *uploads(client, view, parsed.conversation_id), *outputs(client, view, parsed.conversation_id),
            ] if attach else []
            base = Target("thread", parsed.conversation_id, {}).canonical
            content, metadata, prose, authors, part = _render(payload, parsed, base, attach_files=attach, files=files)
            key = parsed.conversation_id + part
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
        if parsed.options.get("output") == "json" and "tool" not in parsed.options:
            content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        transcript = {
            "source": parsed.canonical, "label": metadata.get("title") or parsed.canonical,
            "content": content, "prose": prose, "prose_authors": authors,
            "metadata": {
                **metadata, "provider": PLUGIN_NAME, "kind": parsed.kind,
                "source_ref": "chatgpt", "scope_id": parsed.share_id or parsed.conversation_id or parsed.kind,
                "trace_path": parsed.canonical, "context_subpath": f"chatgpt/{key}.md",
            },
        }
        return [transcript, *[
            _file_document(parsed.conversation_id, file) for file in files if file.content is not None
        ]]


def register_cli_options(command_name: str, command: Any) -> None:
    if command_name not in {"cat", "hydrate", "payload"}:
        return
    import click

    existing = {opt for param in command.params for opt in getattr(param, "opts", ())}
    for name in _CLI_OPTIONS:
        flag = f"--chatgpt-{name}"
        if flag in existing:
            continue
        if name == "map":
            command.params.append(click.Option([flag], is_flag=True, default=False,
                                               help="Outline every version of a ChatGPT conversation."))
            continue
        option_type = (
            click.Choice(["transcript", "json"]) if name == "output"
            else click.Choice(["attach", "inline"]) if name == "files"
            else click.Choice(list(TOOL_MODES)) if name == "tools"
            else click.Choice(list(VARIANT_MODES)) if name == "variants"
            else int if name in _INTEGER_OPTIONS else str
        )
        command.params.append(click.Option([flag], type=option_type, default=None, help=f"ChatGPT {name}; see cx-chats README."))


def collect_cli_overrides(command_name: str, params: dict) -> dict | None:
    if command_name not in {"cat", "hydrate", "payload"}:
        return None
    values = {key: params.get(f"chatgpt_{key}") for key in _CLI_OPTIONS}
    return {key: value for key, value in values.items() if value is not None and value is not False} or None

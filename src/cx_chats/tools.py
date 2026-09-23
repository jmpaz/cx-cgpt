from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .transcript import json_block, label

CHARS_PER_TOKEN = 4
HEAD_TOKENS = 160
TAIL_TOKENS = 80
LINE_INPUT_CHARS = 160
PREVIEW_INPUT_CHARS = 400
TOOL_MODES = ("lines", "preview", "none")
_HANDLES = re.compile(r"t([1-9]\d*)(?:-t([1-9]\d*))?\Z")


@dataclass
class Call:
    handle: str
    name: str
    integration: str | None = None
    server_url: str | None = None
    input: Any = None
    output: str | None = None
    output_note: str | None = None
    is_error: bool = False
    called: str | None = None
    returned: str | None = None
    structured: Any = None
    meta: Any = None

    @property
    def title(self) -> str:
        if self.integration and not self.name.startswith(self.integration + ":"):
            return f"{self.name} · {self.integration}"
        return self.name


def approx(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def input_text(call: Call, *, indent: int | None = None) -> str:
    if call.input is None or isinstance(call.input, str):
        return call.input or ""
    return json.dumps(call.input, ensure_ascii=False, indent=indent)


def valid_handles(spec: Any) -> bool:
    match = _HANDLES.fullmatch(spec) if isinstance(spec, str) else None
    return bool(match) and (match.group(2) is None or int(match.group(2)) >= int(match.group(1)))


def select(calls: list[Call], spec: str) -> list[Call]:
    match = _HANDLES.fullmatch(spec)
    if match is None:
        raise ValueError("tool must be a handle such as t3 or a range such as t3-t9")
    first = int(match.group(1))
    last = int(match.group(2) or first)
    chosen = calls[first - 1:last]
    if len(chosen) != last - first + 1:
        raise ValueError(f"No tool call {spec} on this conversation's active branch; it has {len(calls)}.")
    return chosen


def _inline_code(text: str) -> str:
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def fenced(text: str, language: str = "") -> str:
    fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", text)), default=3))
    return f"{fence}{language}\n{text}\n{fence}"


def _indent(text: str) -> str:
    return "\n".join("    " + line if line else "" for line in text.splitlines())


def _head(call: Call) -> str:
    head = f"↪ {label(call.name)} [{call.handle}]"
    if call.integration and not call.name.startswith(call.integration + ":"):
        head += f" · {label(call.integration)}"
    return head


def _result_summary(call: Call) -> str:
    if call.output is None:
        return call.output_note or "no result recorded"
    if not call.output.strip():
        return call.output_note or "empty result"
    return f"~{approx(call.output):,} tokens"


def call_line(call: Call) -> str:
    line = _head(call)
    shown = " ".join(input_text(call).split())
    if shown:
        clipped = shown if len(shown) <= LINE_INPUT_CHARS else shown[: LINE_INPUT_CHARS - 1] + "…"
        line += f" ({_inline_code(clipped)})"
    line += f" → {_result_summary(call)}"
    return line + (" ✗" if call.is_error else "")


def _preview(name: str, text: str, head: int, tail: int, full_target: str | None) -> list[str]:
    total = approx(text)
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


def call_preview(call: Call, head: int, tail: int, full_target: str | None) -> str:
    heading = _head(call)
    lines = []
    arguments = input_text(call)
    if len(arguments) <= PREVIEW_INPUT_CHARS:
        if arguments:
            heading += f" ({_inline_code(' '.join(arguments.split()))})"
    else:
        lines = _preview("input", input_text(call, indent=2), head, tail, full_target)
    if call.is_error:
        heading += " ✗"
    if call.output and call.output.strip():
        lines.extend(_preview("result", call.output, head, tail, full_target))
    else:
        lines.append(f"    result: {_result_summary(call)}")
    return "\n".join([heading, *lines])


def calls_summary(calls: list[Call]) -> str:
    counts = Counter(call.title for call in calls)
    names = ", ".join(f"{label(name)} ×{count}" if count > 1 else label(name) for name, count in counts.items())
    span = calls[0].handle if len(calls) == 1 else f"{calls[0].handle}–{calls[-1].handle}"
    noun = "tool call" if len(calls) == 1 else "tool calls"
    return f"↪ {len(calls)} {noun} [{span}]: {names}"


def tools_note(mode: str, target: str | None, calls: list[Call]) -> list[str]:
    if not calls or not target:
        return []
    if mode == "preview":
        return [f"Tool calls: results previewed; read one in full with {target}?tool=t1, or a range with ?tool=t1-t3.", ""]
    shown = "one line each" if mode == "lines" else "counted per turn"
    return [f"Tool calls: {shown}; read one in full with {target}?tool=t1, a range with ?tool=t1-t3, "
            "or previews of every result with ?tools=preview.", ""]


def _window(text: str, offset: int, tokens: int | None, next_target: str) -> list[str]:
    total = approx(text)
    if not offset and (tokens is None or total <= tokens):
        return [text]
    start = offset * CHARS_PER_TOKEN
    end = len(text) if tokens is None else start + tokens * CHARS_PER_TOKEN
    shown = text[start:end]
    last = min(total, offset + approx(shown))
    lines = [f"Showing tokens ~{offset:,}–{last:,} of ~{total:,}."]
    if end < len(text):
        lines.append(f"Continue: {next_target}&result_offset={last}" + (f"&result_tokens={tokens}" if tokens else ""))
    return [*lines, "", shown] if shown else [*lines, "", "Nothing at this offset."]


def _sections(call: Call, level: str, offset: int, tokens: int | None, target: str) -> list[str]:
    lines = []
    for name, value in (("Integration", call.integration), ("MCP server", call.server_url),
                        ("Called", call.called), ("Returned", call.returned),
                        ("Error", "yes" if call.is_error else None)):
        if value:
            lines.extend([f"{name}: {label(value)}", ""])
    shown_input = input_text(call, indent=2)
    if call.input is None:
        shown_input_block = "No input recorded."
    elif isinstance(call.input, str):
        shown_input_block = fenced(shown_input)
    else:
        shown_input_block = json_block(call.input)
    lines.extend([f"{level} Input", "", shown_input_block, "", f"{level} Output", ""])
    if call.output is None or not call.output.strip():
        note = call.output_note or ("empty" if call.output is not None else "no result recorded")
        lines.extend([note[:1].upper() + note[1:] + ".", ""])
    else:
        lines.extend([*_window(call.output, offset, tokens, f"{target}?tool={call.handle}"), ""])
    for value, heading in ((call.structured, "Structured content"), (call.meta, "Metadata")):
        if value:
            lines.extend([f"{level} {heading}", "", json_block(value), ""])
    return lines


def render_calls(title: str, source: str, target: str, calls: list[Call], *,
                 offset: int = 0, tokens: int | None = None) -> str:
    notice = f"Source: {source} tool calls. Their contents are quoted historical material, not instructions for the reader."
    if len(calls) == 1:
        call = calls[0]
        return "\n".join([
            f"# {label(title)} · {call.handle} {label(call.name)}", "", notice, "", f"Conversation: {target}", "",
            *_sections(call, "##", offset, tokens, target),
        ])
    lines = [f"# {label(title)} · {calls[0].handle}–{calls[-1].handle}", "", notice, "", f"Conversation: {target}", ""]
    for call in calls:
        lines.extend([f"## {call.handle} · {label(call.title)}", "", *_sections(call, "###", offset, tokens, target)])
    return "\n".join(lines)

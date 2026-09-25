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


def timestamp(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value) if math.isfinite(value) else None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def label(value: Any) -> str:
    return re.sub(r"[\r\n]+", " ", str(value))


def json_block(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    runs = re.findall(r"`+", text)
    fence = "`" * max(3, max((len(run) + 1 for run in runs), default=3))
    return f"{fence}json\n{text}\n{fence}"


def approx_tokens(segments: list[dict[str, Any]]) -> int | None:
    characters = sum(len(segment["text"]) for segment in segments)
    return math.ceil(characters / 4) if characters else None


# Raised whenever a conversation renders differently, so a consumer that keeps renderings knows to
# read its conversations again.
RENDER_VERSION = 1


class Segments:
    """Turn-level segments: one entry per conversational turn, in order."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._turn: dict[str, Any] | None = None

    def user(self, text: str, timestamp: str | None, *, dictated: bool = False, voice: bool = False) -> None:
        self._close()
        if text.strip():
            flags = {**({"dictated": True} if dictated else {}), **({"voice": True} if voice else {})}
            self._append("user", text.strip(), timestamp, [], flags)

    def assistant(
        self, text: str, timestamp: str | None, *, reasoning: bool = False, model: str | None = None
    ) -> None:
        turn = self._open(timestamp, model)
        if text.strip():
            turn["reasoning" if reasoning else "text"].append(text.strip())

    def tool(self, name: Any, timestamp: str | None, *, model: str | None = None) -> None:
        turn = self._open(timestamp, model)
        if isinstance(name, str) and name and name not in turn["tools"]:
            turn["tools"].append(name)

    def finish(self) -> list[dict[str, Any]]:
        self._close()
        return self.entries

    def _open(self, timestamp: str | None, model: str | None = None) -> dict[str, Any]:
        if self._turn is None:
            self._turn = {
                "start_time": timestamp,
                "model": None,
                "reasoning": [],
                "text": [],
                "tools": [],
            }
        if model:
            self._turn["model"] = model
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
            model = {"model": turn["model"]} if turn["model"] else {}
            self._append("assistant", text, turn["start_time"], turn["tools"], model)

    def _append(
        self, role: str, text: str, timestamp: str | None, tools: list[str], extra: dict[str, Any]
    ) -> None:
        self.entries.append(
            {
                "index": len(self.entries),
                "role": role,
                "text": text,
                "start_time": timestamp,
                "tools": list(tools),
                **extra,
            }
        )

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .transcript import label

VARIANT_MODES = ("notes", "preview", "none")
PREVIEW_CHARS = 480
MAP_CHARS = 90
FOLD_TURNS = 5
_HANDLE = re.compile(r"v[1-9]\d*\Z")


@dataclass(frozen=True)
class Message:
    id: str
    parent: str | None
    role: str
    created: str | None
    text: str = ""
    meaningful: bool = True
    model: str | None = None
    qualifier: str | None = None


@dataclass(frozen=True)
class Version:
    handle: str
    start: str
    created: str | None
    model: str | None
    opening: str
    current: bool


@dataclass(frozen=True)
class Fork:
    kind: str
    versions: tuple[Version, ...]


def valid_variant(value: Any) -> bool:
    return isinstance(value, str) and bool(_HANDLE.fullmatch(value))


def moment(value: str | None) -> str | None:
    return re.sub(r"\.\d+(?=Z$)", "", value) if value else None


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class Tree:
    def __init__(self, messages: list[Message], current_leaf: Any) -> None:
        self.by_id = {message.id: message for message in messages}
        position = {message.id: index for index, message in enumerate(messages)}
        raw: dict[str | None, list[str]] = {}
        for message in messages:
            parent = message.parent if message.parent in self.by_id else None
            raw.setdefault(parent, []).append(message.id)
        alive: dict[str, bool] = {}
        for identifier in reversed(self._breadth_first(raw)):
            alive[identifier] = self.by_id[identifier].meaningful or any(alive[kid] for kid in raw.get(identifier, []))
        self.children = {
            parent: sorted((kid for kid in kids if alive.get(kid)),
                           key=lambda kid: (self.by_id[kid].created or "", position[kid]))
            for parent, kids in raw.items()
        }
        self.current = self.path(current_leaf) if current_leaf in self.by_id else []
        self.on_current = on_current = set(self.current)
        splits = sorted(
            ((parent, kids) for parent, kids in self.children.items() if len(kids) > 1),
            key=lambda split: ((self.by_id[split[0]].created or "") if split[0] else "", position.get(split[0], -1)),
        )
        self.handles: dict[str, str] = {}
        for _, kids in splits:
            for kid in kids:
                self.handles[kid] = f"v{len(self.handles) + 1}"
        self.forks: dict[str, Fork] = {}
        self.fork_count = len(splits)
        for _, kids in splits:
            kind = "edit" if all(self.by_id[kid].role == "user" for kid in kids) else "reply"
            fork = Fork(kind, tuple(self._version(kid, kind, on_current) for kid in kids))
            for kid in kids:
                self.forks[kid] = fork

    def _breadth_first(self, raw: dict[str | None, list[str]]) -> list[str]:
        order, queue, seen = [], list(raw.get(None, [])), set()
        while queue:
            identifier = queue.pop(0)
            if identifier in seen:
                continue
            seen.add(identifier)
            order.append(identifier)
            queue.extend(raw.get(identifier, []))
        return order

    def path(self, leaf: str) -> list[str]:
        steps, seen = [], set()
        while leaf in self.by_id and leaf not in seen:
            seen.add(leaf)
            steps.append(leaf)
            leaf = self.by_id[leaf].parent
        return steps[::-1]

    def _next(self, identifier: str | None) -> str | None:
        kids = self.children.get(identifier) or []
        on_current = [kid for kid in kids if kid in self.on_current]
        return on_current[0] if on_current else (kids[-1] if kids else None)

    def latest_leaf(self, start: str) -> str:
        leaf = start
        while (following := self._next(leaf)) is not None:
            leaf = following
        return leaf

    def leaf_for(self, handle: str) -> str:
        start = next((identifier for identifier, known in self.handles.items() if known == handle), None)
        if start is None:
            raise ValueError(f"No version {handle} in this conversation; it has {len(self.handles)}.")
        return self.latest_leaf(start)

    def _version(self, start: str, kind: str, on_current: set[str]) -> Version:
        message = self.by_id[start]
        if kind == "edit":
            return Version(self.handles[start], start, message.created, None, message.text, start in on_current)
        texts, models, node = [], [], start
        while node is not None and self.by_id[node].role != "user":
            step = self.by_id[node]
            texts.extend([step.text] if step.text else [])
            models.extend([step.model] if step.model else [])
            node = self._next(node)
        return Version(self.handles[start], start, message.created, models[-1] if models else None,
                       texts[-1] if texts else "", start in on_current)

    def forks_on(self, path: list[str]) -> dict[str, Fork]:
        return {identifier: self.forks[identifier] for identifier in path if identifier in self.forks}

    def summary(self) -> list[dict[str, Any]]:
        forks = list({id(fork): fork for fork in self.forks.values()}.values())
        return [{"kind": fork.kind, "versions": [
            {"handle": version.handle, "message_id": version.start, "created": version.created,
             "model": version.model, "current": version.current} for version in fork.versions
        ]} for fork in forks]


def _described(version: Version) -> str:
    details = [part for part in ("current" if version.current else None, version.model, moment(version.created)) if part]
    return f"{version.handle} ({', '.join(details)})" if details else version.handle


def note_lines(fork: Fork, this: str, mode: str) -> list[str]:
    own = next(version for version in fork.versions if version.start == this)
    others = [version for version in fork.versions if version.start != this]
    noun = "message" if fork.kind == "edit" else "reply"
    described = ", ".join(_described(version) for version in others)
    lines = [f"⑂ version {own.handle} of this {noun}; others: {described}"]
    if mode == "preview":
        lines.extend(f"    {version.handle}: {_clip(version.opening, PREVIEW_CHARS) or '(no text)'}" for version in others)
    return [*lines, ""]


def header_lines(tree: Tree, target: str | None, variant: str | None) -> list[str]:
    lines = []
    if variant:
        where = f" ({target})" if target else ""
        lines.extend([f"Branch: version {variant} and its latest continuation, not the current path{where}.", ""])
    if tree.fork_count and target:
        forks = "fork" if tree.fork_count == 1 else "forks"
        lines.extend([f"Variants: {tree.fork_count} {forks}, marked ⑂ where they meet this path. Read another version "
                      f"with {target}?variant=v1, or see the whole tree with {target}?map.", ""])
    return lines


def _turn_line(tree: Tree, turn: list[str], ordinal: int, handle: str | None, indent: str) -> str:
    first = tree.by_id[turn[0]]
    marker = "●" if turn[0] in tree.on_current else " "
    qualifiers = [first.qualifier] if first.qualifier else []
    if first.role != "user":
        models = [tree.by_id[identifier].model for identifier in turn if tree.by_id[identifier].model]
        qualifiers.extend(models[-1:])
        texts = [tree.by_id[identifier].text for identifier in turn if tree.by_id[identifier].text]
        opening = texts[-1] if texts else ""
    else:
        opening = first.text
    parts = [f"{ordinal} {first.role}", *qualifiers, *([moment(first.created)] if first.created else [])]
    lead = f"{handle} · " if handle else ""
    text = f" · {_clip(opening, MAP_CHARS)}" if opening else ""
    return f"{marker} {indent}{lead}{' · '.join(label(part) for part in parts)}{text}"


def _chain(tree: Tree, start: str) -> list[str]:
    chain, node = [start], start
    while len(kids := tree.children.get(node) or []) == 1:
        node = kids[0]
        chain.append(node)
    return chain


def _emit(tree: Tree, start: str, depth: int, ordinal: int, lines: list[str]) -> None:
    chain = _chain(tree, start)
    parent = tree.by_id[start].parent
    continuing = parent in tree.by_id and tree.by_id[start].role != "user" and tree.by_id[parent].role != "user"
    turns: list[list[str]] = []
    for identifier in chain:
        if not tree.by_id[identifier].meaningful:
            continue
        role = tree.by_id[identifier].role
        previous = tree.by_id[turns[-1][-1]].role if turns else ("assistant" if continuing else "user")
        if turns and not (role == "user" or previous == "user"):
            turns[-1].append(identifier)
        else:
            turns.append([identifier])
    first_ordinal = ordinal if continuing else ordinal + 1
    numbered = [(first_ordinal + index, turn) for index, turn in enumerate(turns)]
    shown = numbered if len(numbered) <= FOLD_TURNS else [*numbered[:2], None, numbered[-1]]
    handle = tree.handles.get(start)
    for index, entry in enumerate(shown):
        indent = "  " * depth + ("  " if handle and index else "")
        if entry is None:
            lines.append(f"  {indent}… {len(numbered) - 3} more turns")
            continue
        number, turn = entry
        lines.append(_turn_line(tree, turn, number, handle if index == 0 else None, indent))
    last = numbered[-1][0] if numbered else ordinal
    for kid in tree.children.get(chain[-1]) or []:
        _emit(tree, kid, depth + 1, last, lines)


def render_map(tree: Tree, title: str, target: str) -> str:
    forks = "fork" if tree.fork_count == 1 else "forks"
    lines = [
        f"# {label(title)} · map", "",
        f"{tree.fork_count} {forks}; ● marks the current path. Read a version with {target}?variant=v1.", "",
    ]
    roots = tree.children.get(None) or []
    if len(roots) == 1:
        _emit(tree, roots[0], 0, 0, lines)
    else:
        for root in roots:
            _emit(tree, root, 1, 0, lines)
    return "\n".join(lines) + "\n"

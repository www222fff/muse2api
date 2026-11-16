"""Convert a muse.ai agent bubble's innerHTML to markdown.

The page renders the assistant's markdown to HTML, so reading the bubble's
``innerText`` (the old behaviour) returns the *rendered* text: headings lose
their ``#`` markers, list items lose their ``-``/``1.`` markers, code fences
and tables flatten, and zero-width characters the renderer inserts leak into
the reply. The raw markdown is not exposed anywhere else the driver can reach
without a live protocol discovery (no network interception or copy-action
plumbing exists), so the bubble HTML is converted back to markdown here.

The converter is deterministic and stdlib-only: it builds a small element
tree with ``html.parser`` and renders it with fixed rules for headings,
lists, code, tables, links and emphasis. Zero-width characters
(U+200B/U+200C/U+200D/U+FEFF) are stripped everywhere.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
_WS_RUN = re.compile(r"\s+")
_AROUND_NL = re.compile(r" *\n *")
_LANGUAGE = re.compile(r"language-([\w+-]+)")
_VOID = {"br", "hr", "img", "input", "meta", "link", "wbr", "source"}
_DROPPED = {"script", "style", "svg", "button"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_CONTAINERS = {"div", "section", "article", "main", "figure", "figcaption",
                     "details", "summary", "header", "footer"}


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node | str] = []


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self._stack = [self.root]
        self._dropped = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._dropped or tag in _DROPPED:
            self._dropped += 1
            return
        node = _Node(tag, {k: v or "" for k, v in attrs})
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._dropped and tag not in _DROPPED:
            self._stack[-1].children.append(_Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        if self._dropped:
            self._dropped -= 1
            return
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if not self._dropped:
            self._stack[-1].children.append(data)


def _plain_text(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        parts.append(child if isinstance(child, str) else _plain_text(child))
    return "".join(parts)


def _inline(children: list[_Node | str]) -> str:
    parts: list[str] = []
    for child in children:
        if isinstance(child, str):
            parts.append(_WS_RUN.sub(" ", child))
        else:
            parts.append(_inline_node(child))
    text = _AROUND_NL.sub("\n", "".join(parts))
    return text.strip(" ") if "\n" not in text else text.strip()


def _inline_node(node: _Node) -> str:
    tag = node.tag
    if tag in ("strong", "b"):
        inner = _inline(node.children)
        return f"**{inner}**" if inner else ""
    if tag in ("em", "i"):
        inner = _inline(node.children)
        return f"*{inner}*" if inner else ""
    if tag == "code":
        inner = _WS_RUN.sub(" ", _plain_text(node)).strip()
        if not inner:
            return ""
        return f"`` {inner} ``" if "`" in inner else f"`{inner}`"
    if tag == "a":
        inner = _inline(node.children)
        href = node.attrs.get("href", "").strip()
        if not href or href.startswith(("#", "javascript:")) or inner == href:
            return inner
        return f"[{inner}]({href})" if inner else href
    if tag == "img":
        src = node.attrs.get("src", "").strip()
        return f"![{node.attrs.get('alt', '')}]({src})" if src else ""
    if tag == "br":
        return "\n"
    return _inline(node.children)


def _fenced(node: _Node) -> str:
    language = ""
    code = node
    for child in node.children:
        if isinstance(child, _Node) and child.tag == "code":
            code = child
            match = _LANGUAGE.search(child.attrs.get("class", ""))
            language = match.group(1) if match else ""
            break
    body = _plain_text(code).translate(_ZERO_WIDTH).rstrip("\n")
    return f"```{language}\n{body}\n```"


def _list_item_lines(item: _Node, marker: str, depth: int) -> list[str]:
    indent = "  " * depth
    lines: list[str] = []
    inline_run: list[_Node | str] = []
    wrote_first = False

    def flush() -> None:
        nonlocal wrote_first
        text = _inline(inline_run)
        inline_run.clear()
        if text or not wrote_first:
            prefix = indent + marker if not wrote_first else indent + " " * len(marker)
            lines.append(prefix + text if text else prefix.rstrip())
            wrote_first = True

    for part in item.children:
        if isinstance(part, _Node) and part.tag in ("ul", "ol"):
            flush()
            lines.extend(_list_lines(part, depth + 1))
        elif isinstance(part, _Node) and part.tag == "p":
            inline_run.extend(part.children)
        else:
            inline_run.append(part)
    flush()
    return lines


def _list_lines(node: _Node, depth: int) -> list[str]:
    ordered = node.tag == "ol"
    try:
        index = int(node.attrs.get("start", "1")) if ordered else 0
    except ValueError:
        index = 1
    lines: list[str] = []
    for child in node.children:
        if not (isinstance(child, _Node) and child.tag == "li"):
            continue
        marker = f"{index}. " if ordered else "- "
        index += 1
        lines.extend(_list_item_lines(child, marker, depth))
    return lines


def _table(node: _Node) -> str:
    rows: list[list[str]] = []

    def collect(parent: _Node) -> None:
        for child in parent.children:
            if not isinstance(child, _Node):
                continue
            if child.tag == "tr":
                rows.append([_inline(cell.children).replace("|", "\\|")
                             for cell in child.children
                             if isinstance(cell, _Node) and cell.tag in ("th", "td")])
            elif child.tag in ("thead", "tbody", "tfoot"):
                collect(child)

    collect(node)
    rows = [row for row in rows if row]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    lines = ["| " + " | ".join(rows[0] + [""] * (width - len(rows[0]))) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for row in rows[1:]:
        lines.append("| " + " | ".join(row + [""] * (width - len(row))) + " |")
    return "\n".join(lines)


def _block(node: _Node) -> str:
    tag = node.tag
    if tag in _HEADINGS:
        return ("#" * int(tag[1]) + " " + _inline(node.children)).rstrip()
    if tag == "p":
        return _inline(node.children)
    if tag == "pre":
        return _fenced(node)
    if tag in ("ul", "ol"):
        return "\n".join(_list_lines(node, 0))
    if tag == "table":
        return _table(node)
    if tag == "blockquote":
        inner = "\n\n".join(_blocks(node.children))
        return "\n".join("> " + line if line else ">" for line in inner.split("\n"))
    if tag == "hr":
        return "---"
    return "\n\n".join(_blocks(node.children))


def _is_block(node: _Node) -> bool:
    return node.tag in _HEADINGS or node.tag in _BLOCK_CONTAINERS or node.tag in (
        "p", "pre", "ul", "ol", "table", "blockquote", "hr", "li")


def _blocks(children: list[_Node | str]) -> list[str]:
    blocks: list[str] = []
    inline_run: list[_Node | str] = []

    def flush() -> None:
        text = _inline(inline_run)
        inline_run.clear()
        if text:
            blocks.append(text)

    for child in children:
        if isinstance(child, _Node) and _is_block(child):
            flush()
            rendered = _block(child)
            if rendered:
                blocks.append(rendered)
        else:
            inline_run.append(child)
    flush()
    return blocks


def html_to_markdown(html: str) -> str:
    """Render a muse.ai agent bubble's innerHTML as markdown (see module docstring)."""
    if not html:
        return ""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    text = "\n\n".join(_blocks(builder.root.children))
    return text.translate(_ZERO_WIDTH).strip()

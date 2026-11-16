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
            # Void tags never get an end tag, so counting them would keep the
            # dropped subtree open forever and swallow the rest of the reply.
            if tag not in _VOID:
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
            if tag not in _VOID:
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
    """Inline rendering WITHOUT edge stripping: a space at an element's edge
    is a word separator in the flow, and the final bubble often carries it
    inside the inline element (``<strong> displayed </strong>``). Stripping
    it here joined words ("neverdisplayed"); block-level callers strip the
    edges of the whole block instead, and _inline_node moves an element's
    own edge spaces outside its markdown markers."""
    parts: list[str] = []
    for child in children:
        if isinstance(child, str):
            parts.append(_WS_RUN.sub(" ", child))
        else:
            parts.append(_inline_node(child))
    text = ""
    for part in parts:
        # A boundary space may sit on both sides of an element junction
        # (trailing the previous text and leading the element); keep one.
        if text.endswith(" ") and part.startswith(" "):
            part = part[1:]
        text += part
    return _AROUND_NL.sub("\n", text)


def _edge_ws(text: str) -> tuple[str, str, str]:
    """(leading spaces, core, trailing spaces) of an inline element's text."""
    core = text.strip(" ")
    if not core:
        return text, "", ""
    lead = text[: len(text) - len(text.lstrip(" "))]
    trail = text[len(text.rstrip(" ")):]
    return lead, core, trail


def _inline_node(node: _Node) -> str:
    tag = node.tag
    if tag in ("strong", "b"):
        lead, core, trail = _edge_ws(_inline(node.children))
        return f"{lead}**{core}**{trail}" if core else lead
    if tag in ("em", "i"):
        lead, core, trail = _edge_ws(_inline(node.children))
        return f"{lead}*{core}*{trail}" if core else lead
    if tag == "code":
        inner = _WS_RUN.sub(" ", _plain_text(node)).strip()
        if not inner:
            return ""
        return f"`` {inner} ``" if "`" in inner else f"`{inner}`"
    if tag == "a":
        lead, core, trail = _edge_ws(_inline(node.children))
        href = node.attrs.get("href", "").strip()
        if not href or href.startswith(("#", "javascript:")) or core == href:
            return lead + core + trail
        return f"{lead}[{core}]({href}){trail}" if core else lead + href + trail
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
    # The fence must outrun any backtick run inside the body, or a code block
    # that itself contains a fenced block would terminate the outer fence.
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{body}\n{fence}"


_RENDERER_BULLET = re.compile("^[-\\u2022*]\\u200b\\s*")


def _strip_renderer_bullet(node: _Node) -> bool:
    """Remove a renderer bullet glyph from the first text node below ``node``.

    The page's list rendering can leave a literal "-\\u200b" text node inside
    an <li>; converted naively it would duplicate the marker we generate
    ("- -item"). Only the exact artifact shape — a bullet char immediately
    followed by U+200B — is stripped, so a legitimate leading dash survives.
    Returns True once a text node has been processed.
    """
    for i, child in enumerate(node.children):
        if isinstance(child, str):
            if child:
                node.children[i] = _RENDERER_BULLET.sub("", child, count=1)
                return True
            continue
        if _is_block(child):
            return False
        if _strip_renderer_bullet(child):
            return True
    return False


def _list_item_lines(item: _Node, marker: str, depth: int) -> list[str]:
    _strip_renderer_bullet(item)
    indent = "  " * depth
    content_indent = indent + " " * len(marker)
    # Segments: ("text", [line]) for inline runs and paragraphs, ("list",
    # lines) for a nested list, ("block", lines) for any other block child.
    # Each block child keeps its own rendering instead of being flattened
    # into the item's inline text.
    segments: list[tuple[str, list[str]]] = []
    run: list[_Node | str] = []

    def flush_run() -> None:
        text = _inline(run).strip()
        run.clear()
        if text:
            segments.append(("text", [text]))

    for part in item.children:
        if isinstance(part, _Node) and part.tag in ("ul", "ol"):
            flush_run()
            segments.append(("list", _list_lines(part, depth + 1)))
        elif isinstance(part, _Node) and part.tag == "p":
            flush_run()
            text = _inline(part.children).strip()
            if text:
                segments.append(("text", [text]))
        elif isinstance(part, _Node) and _is_block(part):
            flush_run()
            rendered = _block(part)
            if rendered:
                segments.append(("block", rendered.split("\n")))
        else:
            run.append(part)
    flush_run()

    if not segments:
        return [indent + marker.rstrip()]

    def indented(seg_lines: list[str]) -> list[str]:
        return [content_indent + line if line else line for line in seg_lines]

    kind, seg_lines = segments[0]
    if kind == "text":
        lines = [indent + marker + seg_lines[0]]
    elif kind == "list":
        lines = [indent + marker.rstrip(), *seg_lines]
    else:
        lines = [indent + marker.rstrip(), *indented(seg_lines)]
    for i, (kind, seg_lines) in enumerate(segments[1:], start=1):
        if kind == "list":
            # Nested-list lines already carry the depth + 1 indent, which is
            # exactly this item's content column.
            lines.extend(seg_lines)
        elif kind == "text":
            if segments[i - 1][0] in ("text", "block"):
                lines.append("")
            lines.append(content_indent + seg_lines[0])
        else:
            lines.extend(indented(seg_lines))
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
                rows.append([_inline(cell.children).strip().replace("|", "\\|")
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
        return ("#" * int(tag[1]) + " " + _inline(node.children).strip()).rstrip()
    if tag == "p":
        return _inline(node.children).strip()
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
        text = _inline(inline_run).strip()
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

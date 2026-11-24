"""Convert OpenAI-style message lists into a single prompt for the muse web chat.

muse.ai's web UI is a single text box, so multi-turn context and system prompts
are serialised into one message. Image parts are extracted separately so that
drivers can attach them as files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

from .tools import format_tools_prompt

ROLE_LABELS = {
    "system": "System",
    "developer": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool Result",
}


@dataclass
class FlatPrompt:
    text: str
    images: list[str] = field(default_factory=list[str])
    """Image references (data URLs or http(s) URLs) found in the messages, in order."""


def _part_text(part: dict[str, Any]) -> str:
    """Plain text of a single content part (empty for non-text parts)."""
    if part.get("type") in ("text", "input_text", "output_text"):
        return part.get("text", "")
    return ""


def _part_image_ref(part: dict[str, Any]) -> list[str]:
    """Image reference URLs carried by a content part (empty when none)."""
    if part.get("type") not in ("image_url", "input_image"):
        return []
    ref = part.get("image_url")
    if isinstance(ref, dict):
        ref = cast(dict[str, Any], ref).get("url")
    return [ref] if ref else []


def _content_to_text(content: Any, images: list[str]) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            parts.append(str(part))
            continue
        p = cast(dict[str, Any], part)
        images.extend(_part_image_ref(p))
        parts.append(_part_text(p))
    return "\n".join(p for p in parts if p)


def _tool_call_text(tc: Any) -> str | None:
    """Render a single tool_call entry as an XML tool-call line."""
    if not isinstance(tc, dict):
        return None
    fn = cast(dict[str, Any], tc).get("function", {})
    fn_dict = cast(dict[str, Any], fn) if isinstance(fn, dict) else None
    if fn_dict is None:
        return None
    name = fn_dict.get("name", "")
    args = fn_dict.get("arguments", "{}")
    if isinstance(args, dict):
        args = json.dumps(args, ensure_ascii=False)
    return f'<tool_call>\n{{"name": "{name}", "arguments": {args}}}\n</tool_call>'


def _message_to_text(msg: dict[str, Any], images: list[str]) -> str:
    content_text = _content_to_text(msg.get("content"), images).strip()
    role = msg.get("role", "user")
    tool_calls = msg.get("tool_calls")

    parts: list[str] = []
    if content_text:
        parts.append(content_text)

    if isinstance(tool_calls, list):
        for tc in cast(list[Any], tool_calls):
            line = _tool_call_text(tc)
            if line is not None:
                parts.append(line)

    if role == "tool":
        name = msg.get("name") or msg.get("tool_call_id") or "tool"
        prefix = f"[Tool Output ({name})]"
        return f"{prefix}\n" + "\n".join(parts)

    return "\n\n".join(parts)


def message_turns(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Role/text pairs, using the same text extraction as ``flatten_messages``."""
    turns: list[tuple[str, str]] = []
    for msg in messages:
        text = _message_to_text(msg, []).strip()
        if text:
            turns.append((msg.get("role", "user"), text))
    return turns


def followup_text(previous: list[tuple[str, str]], incoming: list[tuple[str, str]]) -> str | None:
    """Text to send when ``incoming`` continues a conversation already on the page.

    Returns only the new user text. ``None`` means the histories do not line up, so the
    caller must open a fresh thread and send the full prompt instead.
    """
    if not previous or len(incoming) <= len(previous):
        return None
    if incoming[: len(previous)] != list(previous):
        return None
    extra = list(incoming[len(previous) :])
    while extra and extra[0][0] in ("assistant", "tool"):
        extra = extra[1:]
    user_text: list[str] = []
    for role, text in extra:
        if role != "user":
            return None
        user_text.append(text)
    return "\n\n".join(user_text) or None


def _is_verbatim_single_turn(turns: list[tuple[str, str]], tool_section: str) -> bool:
    """A lone user turn without tools or workspace context is sent verbatim."""
    return (
        len(turns) == 1
        and turns[0][0] == "user"
        and not tool_section
    )


def _render_blocks(turns: list[tuple[str, str]], tool_section: str) -> list[str]:
    """Render the full multi-turn prompt block list."""
    blocks: list[str] = []
    if tool_section:
        blocks.append(f"[System]\n{tool_section}")

    for role, text in turns:
        label = ROLE_LABELS.get(role, role.title())
        blocks.append(f"[{label}]\n{text}")

    blocks.append(
        "[Instruction]\n"
        "Continue the conversation above and reply as the Assistant to the last User message. "
        "If tool execution results are present in [Tool Result] above, formulate a comprehensive "
        "summary of the tool results for the user. Do not repeat web-sandbox disclaimers. "
        "If another tool call is needed, format it as specified. Output only the reply.",
    )
    return blocks


def flatten_messages(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> FlatPrompt:
    images: list[str] = []
    turns: list[tuple[str, str]] = []
    for msg in messages:
        role = msg.get("role", "user")
        text = _message_to_text(msg, images).strip()
        if text:
            turns.append((role, text))

    if not turns:
        return FlatPrompt(text="", images=images)

    tool_section = format_tools_prompt(tools) if tools else ""

    # The muse web chat has no system role, so system/developer messages are
    # folded, in order, into a delimited block that leads the prompt; buried
    # mid-transcript as "[System]" turns they were ignored by the model.
    system = [text for role, text in turns if role in ("system", "developer")]
    rest = [(role, text) for role, text in turns if role not in ("system", "developer")]
    instructions = ""
    if system:
        instructions = "<system_instructions>\n" + "\n\n".join(system) + "\n</system_instructions>"

    if not rest and not tool_section:
        return FlatPrompt(text=instructions, images=images)

    if _is_verbatim_single_turn(rest, tool_section):
        body = rest[0][1]
    else:
        body = "\n\n".join(_render_blocks(rest, tool_section))
    return FlatPrompt(text=f"{instructions}\n\n{body}" if instructions else body, images=images)

"""DOM selectors and in-page scripts for the muse.ai web client.

All knowledge about the page structure lives here. When muse.ai ships a UI
change, this is normally the only file that needs updating. Scripts return
JSON-serialisable values and never throw (errors are reported in the payload).
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

INPUT = "textarea"
AGENT_BUBBLE = 'div[class*="hatch-agent-bubble-bg"]'
ATTACHMENT = '[data-testid^="hatch-chat-attachment-presentation-"]'
STOP_BUTTON = 'button[aria-label*="Stop" i]'
# The agent asks before acting on the user's behalf (e.g. uploading the result to
# muse.ai/files as a public link). Nobody answers it here, so it would block for minutes.
APPROVAL_CARD = '[data-testid="hatch-inline-approval-card"]'
FILE_INPUT = 'input[type="file"]'

LOGIN_HINTS = ("log in", "sign in", "create an account", "use another account")
QUOTA_HINTS = ("out of credits", "usage limit", "limit reached", "token limit")
STALL_HINTS = ("Still sending", "Connecting...")


def q(value: str) -> str:
    """JSON-quote a Python string for safe interpolation into JS."""
    return json.dumps(value)


PAGE_STATE = f"""(() => {{
  const body = document.body ? document.body.innerText : '';
  const hydration = document.querySelector('[data-hatch-shell-hydration-state]');
  return {{
    ready: document.readyState === 'complete' && !!document.querySelector({q(INPUT)})
           && (!hydration || hydration.getAttribute('data-hatch-shell-hydration-state') === 'hydrated')
           && !body.includes('Connecting...'),
    hasInput: !!document.querySelector({q(INPUT)}),
    path: location.pathname,
    head: body.slice(0, 800),
  }};
}})()"""

# Open a blank thread through the page's own Next.js router. A full Page.navigate
# reloads the app, and the first reply on a reloaded page waits for the workspace
# connection to be re-established. Returns false when the router is not available.
SOFT_NEW_THREAD = """(() => {
  const router = window.next && window.next.router;
  if (!location.hostname.endsWith('muse.ai') || !router || typeof router.push !== 'function') return false;
  router.push('/thread/new');
  return true;
})()"""

# The soft navigation has landed: a ready, empty /thread/new page.
FRESH_THREAD = f"""(() => {{
  const page = {PAGE_STATE};
  return page.ready && page.path === '/thread/new'
         && document.querySelectorAll({q(AGENT_BUBBLE)}).length === 0;
}})()"""

# Snapshot of the conversation used to detect new output after sending.
CHAT_STATE = f"""(() => {{
  const scrollers = [...document.querySelectorAll('*')].filter(e => {{
    const s = getComputedStyle(e);
    return (s.overflowY === 'auto' || s.overflowY === 'scroll') && e.scrollHeight > e.clientHeight + 50;
  }});
  scrollers.forEach(e => {{ e.scrollTop = e.scrollHeight; }});
  const bubbles = [...document.querySelectorAll({q(AGENT_BUBBLE)})];
  const last = bubbles.length ? (bubbles[bubbles.length - 1].innerText || '').trim() : '';
  const lastHtml = bubbles.length ? (bubbles[bubbles.length - 1].innerHTML || '') : '';
  const atts = [...document.querySelectorAll({q(ATTACHMENT)})].map(a => {{
    const v = a.querySelector('video'), i = a.querySelector('img');
    const m = v || i;
    return {{
      tid: a.getAttribute('data-testid') || '',
      kind: v ? 'video' : (i ? 'image' : 'unknown'),
      src: m ? (m.currentSrc || m.src || '') : '',
      w: m ? (m.videoWidth || m.naturalWidth || 0) : 0,
      h: m ? (m.videoHeight || m.naturalHeight || 0) : 0,
    }};
  }});
  const tail = document.body ? document.body.innerText.slice(-600) : '';
  return {{ agentCount: bubbles.length, lastText: last, lastHtml,
            generating: !!document.querySelector({q(STOP_BUTTON)}),
            approval: !!document.querySelector({q(APPROVAL_CARD)}),
            attachments: atts, tail }};
}})()"""


# Click "Deny" on every pending approval card; the agent then carries on without
# the action (and the result stays private instead of becoming a public link).
DENY_APPROVALS = f"""(() => {{
  const denied = [];
  for (const card of document.querySelectorAll({q(APPROVAL_CARD)})) {{
    const btn = [...card.querySelectorAll('button,[role=button]')]
      .find(b => /^(deny|decline|reject|don.t allow)$/i.test((b.innerText || '').trim()));
    if (btn && !btn.disabled) {{
      btn.click();
      denied.push(card.getAttribute('aria-label') || 'approval request');
    }}
  }}
  return denied;
}})()"""


# Media links that the assistant delivered as a text bubble / anchor / <video>
# element rather than as an inline attachment presentation. Scans the last agent
# bubble for every candidate URL (anchor hrefs, media element srcs incl. blob:,
# and bare URLs in the text) and returns the bubble HTML for diagnostics.
LAST_BUBBLE_MEDIA = f"""(() => {{
  const bubbles = [...document.querySelectorAll({q(AGENT_BUBBLE)})];
  const b = bubbles.length ? bubbles[bubbles.length - 1] : null;
  if (!b) return {{ links: [], html: '' }};
  const urls = [];
  const push = (u) => {{ if (u && !urls.includes(u)) urls.push(u); }};
  b.querySelectorAll('video, source').forEach(m => push(m.currentSrc || m.src || ''));
  b.querySelectorAll('a[href]').forEach(a => push(a.href || ''));
  b.querySelectorAll('img').forEach(m => push(m.currentSrc || m.src || ''));
  const text = b.innerText || '';
  const re = /https?:\\/\\/[^\\s)<>"']+/g;
  let mm; while ((mm = re.exec(text))) push(mm[0]);
  return {{ links: urls, html: (b.outerHTML || '').slice(0, 2000) }};
}})()"""


def fill_input(text: str) -> str:
    """Set the textarea value through the native setter so React picks it up."""
    return f"""(() => {{
  const ta = document.querySelector({q(INPUT)});
  if (!ta) return {{ ok: false, err: 'no-input' }};
  ta.focus();
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, {q(text)});
  ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
  return {{ ok: true, len: ta.value.length }};
}})()"""


CLICK_SEND = """(() => {
  const btn = [...document.querySelectorAll('button,[role=button]')]
    .filter(b => b.offsetParent !== null)
    .find(b => /send|发送|發送|submit/i.test((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '')));
  if (!btn) return 'missing';
  if (btn.disabled) return 'disabled';
  btn.click();
  return 'clicked';
})()"""

INPUT_EMPTY = f"!(document.querySelector({q(INPUT)}) || {{value: ''}}).value"


def attach_file(b64: str, mime: str, filename: str) -> str:
    return f"""(() => {{
  try {{
    const input = document.querySelector({q(FILE_INPUT)});
    if (!input) return {{ ok: false, err: 'no-file-input' }};
    const bin = atob({q(b64)});
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], {q(filename)}, {{ type: {q(mime)} }}));
    input.files = dt.files;
    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
    return {{ ok: true }};
  }} catch (e) {{ return {{ ok: false, err: String(e) }}; }}
}})()"""


def fetch_as_base64(url: str) -> str:
    """Fetch a (possibly blob:) URL inside the page context and return base64 bytes."""
    return f"""(async () => {{
  try {{
    const r = await fetch({q(url)});
    const b = await r.blob();
    const buf = new Uint8Array(await b.arrayBuffer());
    let s = '';
    for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    return {{ ok: true, mime: b.type || '', b64: btoa(s) }};
  }} catch (e) {{ return {{ ok: false, err: String(e) }}; }}
}})()"""


# ---------------------------------------------------------------- quota
# The quota lives in the dock's Settings menu -> Settings -> General -> Usage:
#   Usage / Free plan / Weekly limit resets on Sep 30 / 1% used /
#   Additional tokens / Never expires / 0% used (2B tokens left) / Language
# Labels are matched in English, simplified and traditional Chinese.
SETTINGS_LABELS = ("Settings", "設定", "设置")
SETTINGS_DOCK = '[data-testid="hatch-dock-more"]'

_VISIBLE = ("const visible = e => e.getClientRects().length > 0"
            " && getComputedStyle(e).visibility !== 'hidden';")
_CENTER = ("if (!b) return null; const r = b.getBoundingClientRect();"
           " return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};")

# Centre of the dock's Settings ("more") button, or null.
QUOTA_SETTINGS_ENTRY = f"""(() => {{
  {_VISIBLE}
  const labels = {json.dumps(list(SETTINGS_LABELS), ensure_ascii=False)};
  const b = [...document.querySelectorAll({q(SETTINGS_DOCK)})].find(visible)
    || [...document.querySelectorAll('button,[role=button]')].find(e =>
         visible(e) && labels.includes((e.getAttribute('aria-label') || '').trim()));
  {_CENTER}
}})()"""

# Centre of the "Settings" item in the menu that entry opens, or null. Items can
# carry a shortcut suffix (e.g. "設定Ctrl+,"), so a child with the exact label counts.
QUOTA_MENU_ITEM = f"""(() => {{
  {_VISIBLE}
  const labels = {json.dumps(list(SETTINGS_LABELS), ensure_ascii=False)};
  const exact = e => labels.includes((e.textContent || '').trim());
  const b = [...document.querySelectorAll('[role=menuitem]')].find(e =>
      visible(e) && (exact(e) || [...e.querySelectorAll('span,div')].some(exact)))
    || [...document.querySelectorAll('[role=menu] button,[role=menu] span,[role=menu] div')]
         .find(e => visible(e) && exact(e));
  {_CENTER}
}})()"""

# Text of the visible settings dialog ('' when none is open).
QUOTA_DIALOG_TEXT = """(() => {
  const d = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')]
    .find(e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden');
  return d ? (d.innerText || '') : '';
})()"""

_USAGE_HEADINGS = ("usage", "用量", "使用量")
_LANGUAGE_HEADINGS = ("language", "语言", "語言")
_PLAN = re.compile(r"(?:\bplan\b|計劃|计划|方案)", re.IGNORECASE)
_RESET_EN = re.compile(r"Weekly limit resets? on ([^\n]+)", re.IGNORECASE)
_RESET_ZH = re.compile(r"(?:每(?:週|周|星期)|週|周)(?:上限|限額|限额|額度|额度)"
                       r"(?:會|会|將|将)?(?:於|于|在)\s*([^\n]*?)\s*(?:重設|重置)\b")
_EXTRA = re.compile(r"^(?:(?:Additional|Extra)\s+tokens|(?:額外|额外)(?:的)?"
                    r"(?:符元|符記|代幣|代币|令牌|憑證|凭证|tokens))", re.IGNORECASE)
_PCT_USED = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:used|已用|已使用)", re.IGNORECASE)
_USED_PCT = re.compile(r"已(?:使)?用\s*(\d+(?:\.\d+)?)\s*%")


def _used_pct(line: str) -> float | None:
    m = _PCT_USED.search(line) or _USED_PCT.search(line)
    if not m:
        return None
    value = float(m.group(1))
    return value if 0 <= value <= 100 else None


def _usage_section(text: str | None) -> list[str]:
    """Return the dialog lines between the Usage and Language headings."""
    normalized = unicodedata.normalize("NFKC", text or "")
    lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
    start = next((i + 1 for i, ln in enumerate(lines) if ln.lower() in _USAGE_HEADINGS), 0)
    lines = lines[start:]
    end = next((i for i, ln in enumerate(lines) if ln.lower() in _LANGUAGE_HEADINGS), len(lines))
    return lines[:end]


def _extra_tokens_left(lines: list[str]) -> str | None:
    """Extract the leftover 'additional tokens' reading from a line list."""
    for ln in lines:
        if m := re.search(r"\(([^)]+)\)", ln):
            return m.group(1).strip()
        if _used_pct(ln) is not None:
            continue
        if re.search(r"\b\d+[A-Z]?\s+(?:tokens?|令牌|憑證|代幣)"
                     r"(?:\s+(?:left|remaining))?\b", ln, re.IGNORECASE):
            return ln.strip()
    return None


def parse_usage_text(text: str | None) -> dict[str, Any]:
    """Parse the settings dialog's Usage section (English / zh-CN / zh-TW).

    Returns ``plan``, ``weekly_used_pct``, ``resets_at``, ``extra_left`` (each None when
    absent) and ``found``, which is True only when the weekly usage percentage was
    read: a plan label alone, or only the extra-token line, is not a quota reading."""
    lines = _usage_section(text)
    joined = "\n".join(lines)

    out: dict[str, Any] = {"plan": None, "weekly_used_pct": None, "resets_at": None,
                           "extra_left": None}
    out["plan"] = next((ln for ln in lines if _PLAN.search(ln)), None)
    if reset := (_RESET_EN.search(joined) or _RESET_ZH.search(joined)):
        out["resets_at"] = reset.group(1).strip()
    extra_at = next((i for i, ln in enumerate(lines) if _EXTRA.match(ln)), len(lines))
    out["weekly_used_pct"] = next(
        (v for ln in lines[:extra_at] if (v := _used_pct(ln)) is not None), None)
    out["extra_left"] = _extra_tokens_left(lines[extra_at:])
    out["found"] = out["weekly_used_pct"] is not None
    return out

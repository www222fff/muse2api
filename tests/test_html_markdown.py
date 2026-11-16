"""The bubble's innerText is the *rendered* text: markdown markers are lost
(``## `` headings, list markers, code fences, tables) and zero-width renderer
artifacts leak in. These tests pin the HTML -> markdown conversion the browser
driver now applies to the bubble's innerHTML, plus the driver wiring that
feeds the converted text into the chat stream.
"""

from __future__ import annotations

from muse2api.drivers.browser.html_markdown import html_to_markdown

ZERO_WIDTH = "\u200b\u200c\u200d\ufeff"
HEADINGS = ("## Background / Goal", "## Scope (in / out)",
            "## Functional requirements", "## Acceptance criteria")

TEMPLATE_HTML = """
<h2>Background / Goal</h2>
<p>The portal needs a login page so staff can reach the dashboard.</p>
<h2>Scope (in / out)</h2>
<p>In scope: SSO sign-in. Out of scope: billing.</p>
<h2>Functional requirements</h2>
<ul><li>FR-1: the user can sign in with SSO</li><li>FR-2: sessions expire after 30 minutes</li></ul>
<h2>Acceptance criteria</h2>
<p>Given a signed-out user, when they sign in, then they land on the dashboard.</p>
"""


def test_template_headings_round_trip_verbatim():
    md = html_to_markdown(TEMPLATE_HTML)
    for heading in HEADINGS:
        assert md.count(heading) == 1, md
    assert md.startswith("## Background / Goal")
    assert "- FR-1: the user can sign in with SSO" in md


def test_heading_levels():
    assert html_to_markdown("<h1>Top</h1><h3>Deep</h3>") == "# Top\n\n### Deep"


def test_nested_and_ordered_lists():
    html = ("<ul><li>Alpha<ul><li>Alpha one</li><li>Alpha two</li></ul></li>"
            "<li>Beta</li></ul><ol><li>First</li><li>Second</li></ol>")
    assert html_to_markdown(html) == (
        "- Alpha\n  - Alpha one\n  - Alpha two\n- Beta\n\n1. First\n2. Second"
    )


def test_code_block_and_inline_code():
    html = ('<pre><code class="language-python">def f():\n'
            '    return "## not a heading"</code></pre>'
            "<p>Call <code>f()</code> now.</p>")
    md = html_to_markdown(html)
    assert md == ('```python\ndef f():\n    return "## not a heading"\n```'
                  "\n\nCall `f()` now.")
    assert md.count("## not a heading") == 1  # stays literal inside the fence


def test_table():
    html = ("<table><thead><tr><th>Name</th><th>Role</th></tr></thead>"
            "<tbody><tr><td>Ana</td><td>Dev</td></tr>"
            "<tr><td>Bo</td><td>Ops</td></tr></tbody></table>")
    assert html_to_markdown(html) == (
        "| Name | Role |\n| --- | --- |\n| Ana | Dev |\n| Bo | Ops |"
    )


def test_emphasis_links_and_entities():
    html = ('<p>Use <strong>care</strong> and <em>speed</em>; see '
            '<a href="https://example.com/docs">the docs</a>. Fish &amp; Chips.</p>')
    assert html_to_markdown(html) == (
        "Use **care** and *speed*; see [the docs](https://example.com/docs). "
        "Fish & Chips."
    )


def test_zero_width_characters_are_stripped():
    html = "<p>Hel\u200blo wor\u200dld</p><ul><li>\u200bPoint one</li></ul>"
    md = html_to_markdown(html)
    assert md == "Hello world\n\n- Point one"
    assert not any(char in md for char in ZERO_WIDTH)


def test_blockquote_and_rule():
    html = "<blockquote><p>quoted words</p></blockquote><hr><p>after</p>"
    assert html_to_markdown(html) == "> quoted words\n\n---\n\nafter"


async def test_driver_state_converts_bubble_html_to_markdown(tmp_path):
    from muse2api.config import Settings
    from muse2api.drivers.browser import dom
    from muse2api.drivers.browser.driver import BrowserDriver, _Tab

    class _Session:
        async def evaluate(self, expr, **_):
            if expr == dom.CHAT_STATE:
                return {"agentCount": 1, "generating": False, "tail": "",
                        "lastText": "Background / Goal rendered text",
                        "lastHtml": TEMPLATE_HTML, "attachments": []}
            return {}

    driver = BrowserDriver(Settings(_env_file=None, data_dir=tmp_path))
    state = await driver._state(_Tab("a0", "ctx", "t", _Session()))
    for heading in HEADINGS:
        assert state["lastText"].count(heading) == 1

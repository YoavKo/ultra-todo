"""Markdown for ticket descriptions and comments (rendered in the web UI).

CommonMark plus tables, strikethrough, task lists and bare-URL links. Raw HTML in the text is
escaped, never passed through, and unsafe link schemes (javascript:, data:, ...) are not
linked, so user text cannot inject markup. A newline is a line break, as plain-text comments
always were. Blocks get dir="auto" so Hebrew and English paragraphs each face the right way.
"""
from __future__ import annotations

import re
from typing import Callable

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.rules_core import StateCore
from markupsafe import Markup
from mdit_py_plugins.tasklists import tasklists_plugin

_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_AUTO_DIR = {"paragraph_open", "heading_open", "list_item_open", "blockquote_open", "th_open", "td_open"}


def _target(href: str, env: dict) -> str:
    """Point a bare file name (`shot.png`, `attachments/description/shot.png`) at the attachment."""
    resolve: Callable[[str], str | None] | None = env.get("resolve")
    if resolve and href and not _SCHEME.match(href) and not href.startswith(("/", "#")):
        return resolve(href) or href
    return href


def _post(state: StateCore) -> None:
    for token in state.tokens:
        if token.type in _AUTO_DIR:
            token.attrSet("dir", "auto")
        for child in token.children or []:
            if child.type == "link_open":
                href = child.attrGet("href") or ""
                child.attrSet("href", _target(href, state.env))
                if _SCHEME.match(href) and not href.lower().startswith("mailto:"):
                    child.attrSet("rel", "noopener noreferrer")
                    child.attrSet("target", "_blank")
            elif child.type == "image":
                src = child.attrGet("src") or ""
                child.attrSet("src", _target(src, state.env))
                child.attrSet("loading", "lazy")
                child.attrSet("referrerpolicy", "no-referrer")


def _fence(self, tokens, idx, options, env):
    """A ```mermaid block becomes <pre class="mermaid"> with the escaped source; the page's
    script turns it into a diagram (and leaves the text if the diagram does not parse)."""
    token = tokens[idx]
    if (token.info.split() or [""])[0] == "mermaid":
        return f'<pre class="mermaid" dir="ltr">{escapeHtml(token.content)}</pre>\n'
    return self._plain_fence(tokens, idx, options, env)


def _make() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "breaks": True})
    md.renderer._plain_fence = md.renderer.fence
    md.add_render_rule("fence", _fence)
    md.enable(["table", "strikethrough", "linkify"])
    md.use(tasklists_plugin)
    md.core.ruler.push("ultra_todo_post", _post)
    return md


_md = _make()


def render(text: str | None, resolve: Callable[[str], str | None] | None = None) -> Markup:
    """HTML for `text`. `resolve` maps a relative file reference to an attachment URL (or None)."""
    return Markup(_md.render(text or "", {"resolve": resolve}))

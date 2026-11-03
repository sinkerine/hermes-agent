"""Pure Matrix HTML sanitization, Markdown rendering and Element math markup."""

from __future__ import annotations

from contextlib import suppress
from html import escape as _html_escape
from html.parser import HTMLParser
import re


class _MatrixHtmlSanitizer(HTMLParser):
    """Allowlist sanitizer for Matrix-compatible formatted HTML."""

    _ALLOWED_TAGS = {
        "a", "b", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "li", "ol",
        "p", "pre", "s", "strike", "strong", "table", "tbody", "td", "th", "thead", "tr", "ul"}
    _VOID_TAGS = {"br", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._parts: list[str] = []
        self._skip_depth = 0

    @staticmethod
    def _safe_url(value: str) -> str:
        stripped = re.sub(r"[\x00-\x1f\x7f]+", "", value or "").strip()
        match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", stripped)
        scheme = match.group(1).lower() if match else ""
        if scheme and scheme not in {"http", "https", "matrix", "mailto"}:
            return ""
        return stripped

    def _safe_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        safe: list[str] = []
        for key, value in attrs:
            attr = str(key or "").lower()
            raw_value = "" if value is None else str(value)
            if tag == "a" and attr == "href":
                href = self._safe_url(raw_value)
                if href:
                    safe.append(f' href="{_html_escape(href, quote=True)}"')
            elif tag == "code" and attr == "class" and re.fullmatch(r"language-[A-Za-z0-9_+.-]{1,64}", raw_value):
                safe.append(f' class="{_html_escape(raw_value, quote=True)}"')
        return "".join(safe)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif not self._skip_depth and tag in self._ALLOWED_TAGS:
            self._parts.append(f"<{tag}>" if tag in self._VOID_TAGS else f"<{tag}{self._safe_attrs(tag, attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth or tag not in self._ALLOWED_TAGS or tag in self._VOID_TAGS:
            return
        self._parts.append(f"</{tag}>")

    def _emit(self, text: str) -> None:
        if not self._skip_depth:
            self._parts.append(text)

    def handle_data(self, data: str) -> None:
        self._emit(_html_escape(data))

    def handle_entityref(self, name: str) -> None:
        self._emit(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._emit(f"&#{name};")

    def get_html(self) -> str:
        return "".join(self._parts)


# --- LaTeX math ($...$, $$...$$) -> Element data-mx-maths markup ---
# Element (feature_latex_maths) typesets <div|span data-mx-maths="TEX"> at display time.
# Our sanitizer allowlists tags/attrs, so data-mx-maths cannot pass through HTML
# sanitization directly. Instead, math is swapped for opaque sentinel tokens before
# Markdown conversion (protecting TeX from escaping) and expanded back to math
# markup after sanitization. Tokens are plain printable text with no special
# HTML/Markdown meaning, so both the Markdown converter and the sanitizer
# pass them through verbatim.
_TEX_TOKEN_RE = re.compile(r"HERMESTEX(?:DISPLAY|INLINE)(\d+)HERMESTEXEND")
_TEX_DISPLAY_TOKEN = "HERMESTEXDISPLAY%dHERMESTEXEND"
_TEX_INLINE_TOKEN = "HERMESTEXINLINE%dHERMESTEXEND"


def _latex_to_tokens(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace ``$$...$$``/``$...$`` with sentinel tokens.

    Returns the tokenized text plus an ordered ``(tag, tex)`` store, where tag
    is ``div`` for display math and ``span`` for inline math. Dollars that do
    not form a pair (prices, literals) are left untouched.
    """
    if not text or "$" not in text:
        return text, []
    store: list[tuple[str, str]] = []

    def _sub_display(match: re.Match[str]) -> str:
        store.append(("div", match.group(1).strip()))
        return _TEX_DISPLAY_TOKEN % (len(store) - 1)

    def _sub_inline(match: re.Match[str]) -> str:
        store.append(("span", match.group(1).strip()))
        return _TEX_INLINE_TOKEN % (len(store) - 1)

    text = re.sub(r"\$\$([^\n$]+?)\$\$", _sub_display, text)
    text = re.sub(r"(?<![\\$\w])\$([^\n$]+?)\$(?!\w)", _sub_inline, text)
    return text, store


def _tokens_to_mx_maths(html: str, store: list[tuple[str, str]]) -> str:
    """Expand sentinel tokens into ``data-mx-maths`` markup (TeX HTML-escaped)."""

    def _expand(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        if idx >= len(store):
            # Not one of our tokens (user-typed text that collides with the
            # sentinel format) — leave it verbatim.
            return match.group(0)
        tag, tex = store[idx]
        escaped = _html_escape(tex, quote=True)
        return f'<{tag} data-mx-maths="{escaped}">{escaped}</{tag}>'

    return _TEX_TOKEN_RE.sub(_expand, html)

def sanitize_matrix_html(html: str) -> str:
    sanitizer = _MatrixHtmlSanitizer()
    try:
        sanitizer.feed(html or "")
        sanitizer.close()
        return sanitizer.get_html()
    except Exception:
        return _html_escape(html or "")


def _pre_sanitize_matrix_markdown(text: str) -> str:
    """Remove unsafe raw HTML before Markdown conversion can escape it."""
    result = re.sub(r"(?is)<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", "", text or "")
    result = re.sub(r"""(?is)\s+on[a-z0-9_-]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", "", result)
    return re.sub(
        r"""(?is)\s+(href|src)\s*=\s*("[^"]*(?:javascript|data|vbscript):[^"]*"|'[^']*(?:javascript|data|vbscript):[^']*'|[^\s>]*(?:javascript|data|vbscript):[^\s>]*)""",
        "", result)


def markdown_to_matrix_html(text: str) -> str:
    """Markdown → org.matrix.custom.html via ``markdown`` when installed, else the regex fallback."""
    text = _pre_sanitize_matrix_markdown(text)
    text, tex_store = _latex_to_tokens(text)
    with suppress(ImportError):
        import markdown as _md
        md = _md.Markdown(extensions=["fenced_code", "tables", "nl2br", "sane_lists"])
        if "html_block" in md.preprocessors:
            md.preprocessors.deregister("html_block")
        html = md.convert(text)
        md.reset()
        if html.count("<p>") == 1:
            html = html.replace("<p>", "").replace("</p>", "")
        return _tokens_to_mx_maths(sanitize_matrix_html(html), tex_store)
    return _tokens_to_mx_maths(sanitize_matrix_html(_markdown_to_html_fallback(text)), tex_store)


def _sanitize_link_url(url: str) -> str:
    stripped = url.strip()
    if ":" in stripped and stripped.split(":", 1)[0].lower().strip() in {"javascript", "data", "vbscript"}:
        return ""
    return stripped.replace('"', "&quot;")

def _markdown_to_html_fallback(text: str) -> str:
    """Comprehensive regex Markdown-to-HTML for Matrix."""
    placeholders: list = []

    def _is_bq_line(ln: str) -> bool:
        return ln.startswith(("&gt; ", "> ")) or ln in ("&gt;", ">")

    def _protect_html(html_fragment: str) -> str:
        idx = len(placeholders)
        placeholders.append(html_fragment)
        return f"\x00PROTECTED{idx}\x00"

    result = re.sub(
        r"```(\w*)\n(.*?)```",
        lambda m: _protect_html(
            f'<pre><code class="language-{_html_escape(m.group(1))}">{_html_escape(m.group(2))}</code></pre>'
            if m.group(1) else f"<pre><code>{_html_escape(m.group(2))}</code></pre>"),
        text, flags=re.DOTALL)
    result = re.sub(r"`([^`\n]+)`", lambda m: _protect_html(f"<code>{_html_escape(m.group(1))}</code>"), result)
    # Protect markdown links before escaping.
    result = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: _protect_html(
            f'<a href="{_sanitize_link_url(m.group(2))}">{_html_escape(m.group(1))}</a>'),
        result)
    result = "".join(p if p.startswith("\x00PROTECTED") else _html_escape(p)
                     for p in re.split(r"(\x00PROTECTED\d+\x00)", result))
    # Block-level transforms (line-oriented): hr, headers, blockquote, lists.
    lines = result.split("\n")
    out_lines: list = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^[\s]*([-*_])\s*\1\s*\1[\s\-*_]*$", line):
            out_lines.append("<hr>")
            i += 1
            continue
        hdr = re.match(r"^(#{1,6})\s+(.+)$", line)
        if hdr:
            level = len(hdr.group(1))
            out_lines.append(f"<h{level}>{hdr.group(2).strip()}</h{level}>")
            i += 1
            continue
        if _is_bq_line(line):
            bq_lines = []
            while i < len(lines) and _is_bq_line(lines[i]):
                ln = lines[i]
                bq_lines.append(ln[5:] if ln.startswith("&gt; ") else ln[2:] if ln.startswith("> ") else "")
                i += 1
            out_lines.append(f"<blockquote>{'<br>'.join(bq_lines)}</blockquote>")
            continue
        for item_re, tag in ((r"^[\s]*[-*+]\s+(.+)$", "ul"), (r"^[\s]*\d+[.)]\s+(.+)$", "ol")):
            if re.match(item_re, line):
                items = []
                while i < len(lines) and re.match(item_re, lines[i]):
                    items.append(re.match(item_re, lines[i]).group(1))
                    i += 1
                out_lines.append(f"<{tag}>{''.join(f'<li>{item}</li>' for item in items)}</{tag}>")
                break
        else:
            out_lines.append(line)
            i += 1
    result = "\n".join(out_lines)
    for pattern, repl in (
        (r"\*\*(.+?)\*\*", r"<strong>\1</strong>"), (r"__(.+?)__", r"<strong>\1</strong>"),
        (r"\*(.+?)\*", r"<em>\1</em>"), (r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>"),
        (r"~~(.+?)~~", r"<del>\1</del>")):
        result = re.sub(pattern, repl, result, flags=re.DOTALL)
    result = re.sub(r"\n", "<br>\n", result)
    result = re.sub(r"<br>\n(</?(?:pre|blockquote|h[1-6]|ul|ol|li|hr))", r"\n\1", result)
    result = re.sub(r"(</(?:pre|blockquote|h[1-6]|ul|ol|li)>)<br>", r"\1", result)
    for idx, original in enumerate(placeholders):
        result = result.replace(f"\x00PROTECTED{idx}\x00", original)
    return result

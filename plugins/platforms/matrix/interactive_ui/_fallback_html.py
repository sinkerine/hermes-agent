"""HTML for the small Markdown vocabulary emitted by the UI fallback renderer."""

from html import escape, unescape
import re


_ESCAPED_PUNCTUATION = r"\\([\\`*_{}\[\]()#+.!|~\-])"


def _literal(text: str) -> str:
    # Decode exactly one renderer escape layer, then make every source tag inert.
    return escape(unescape(re.sub(_ESCAPED_PUNCTUATION, r"\1", text)))


def _inline(text: str) -> str:
    parts = []
    literal = []
    strong = False
    # Consume escaped punctuation before recognizing the table's bold delimiters.
    for token in re.split(r"(\\[\\`*_{}\[\]()#+.!|~\-]|\*\*)", text):
        if token == "**":
            parts.extend((_literal("".join(literal)), "</strong>" if strong else "<strong>"))
            literal = []
            strong = not strong
        else:
            literal.append(token)
    parts.append(_literal("".join(literal)))
    if strong:
        parts.append("</strong>")
    return "".join(parts)


def _block(text: str) -> str:
    if text == "---":
        return "<hr>"
    heading = re.match(r"^(#{1,6}) (.*)$", text, re.DOTALL)
    if heading:
        level = len(heading[1])
        return f"<h{level}>{_inline(heading[2])}</h{level}>"
    item = re.match(r"^(- |[0-9]+\. )(.*)$", text, re.DOTALL)
    if item:
        tag = "ul" if item[1] == "- " else "ol"
        body = _inline(item[2]).replace("\n", "<br>\n")
        return f"<{tag}><li>{body}</li></{tag}>"
    return "<p>" + _inline(text).replace("\n", "<br>\n") + "</p>"


def generated_markdown_to_html(text: str) -> str:
    """Convert only renderer-generated structure; escaped user text stays literal."""
    blocks = []
    ordered_items = []
    previous_number = 0
    for block in text.split("\n\n"):
        item = re.match(r"^([0-9]+)\. (.*)$", block, re.DOTALL)
        # Each generated list starts at 1, including adjacent lists without a label.
        if ordered_items and (not item or int(item[1]) != previous_number + 1):
            blocks.append("<ol>" + "".join(ordered_items) + "</ol>")
            ordered_items = []
        if item:
            previous_number = int(item[1])
            body = _inline(item[2]).replace("\n", "<br>\n")
            ordered_items.append(f"<li>{body}</li>")
        else:
            blocks.append(_block(block))
    if ordered_items:
        blocks.append("<ol>" + "".join(ordered_items) + "</ol>")
    return "\n\n".join(blocks)

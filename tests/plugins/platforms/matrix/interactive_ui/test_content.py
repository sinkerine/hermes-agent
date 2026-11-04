"""Whole-event budgets and Matrix message/edit wire behavior."""

from copy import deepcopy
from html import unescape
from html.parser import HTMLParser
import json
import re

import pytest

from plugins.platforms.matrix.adapter_html import markdown_to_matrix_html, sanitize_matrix_html
from plugins.platforms.matrix.interactive_ui import INTERACTIVE_UI_KEY
from plugins.platforms.matrix.interactive_ui.content import build_ui_content, build_ui_edit
from plugins.platforms.matrix.interactive_ui.fallback import render_fallback_markdown
from plugins.platforms.matrix.interactive_ui.protocol import InteractiveUiError, ParsedUi, parse_interactive_ui


def payload():
    return {"version": 1, "ui_id": "card", "revision": 0, "status": "active", "title": "Complete answer",
            "state": {}, "root": "root", "nodes": [{"id": "root", "type": "rich_text", "text": "Plain text",
            "html": '<p onclick="bad()">Rich <b>text</b><script>bad()</script><a href="javascript:bad()">link</a></p>'}]}


def build(raw=None, fallback=None, thread=None, **kwargs):
    raw = payload() if raw is None else raw
    return build_ui_content(raw, render_fallback_markdown(raw) if fallback is None else fallback,
                            to_html=kwargs.get("to_html"),
                            sanitize_html=kwargs.get("sanitize_html", sanitize_matrix_html), thread_metadata=thread)


def wire_size(content):
    # Independent wire serialization, rather than the production byte-count helper.
    return len(json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def assert_too_large(result, limit):
    assert isinstance(result, InteractiveUiError), result
    assert result.path == ""
    assert result.code == "too_large"
    assert str(limit) in result.hint


def test_new_event_is_complete_sanitized_and_does_not_mutate_input():
    raw = payload()
    original = deepcopy(raw)
    result = build(raw)
    assert isinstance(result, dict)
    assert result["msgtype"] == "m.text"
    assert result["format"] == "org.matrix.custom.html"
    assert result["m.mentions"] == {}
    assert "m.relates_to" not in result
    assert result["body"] == render_fallback_markdown(raw)
    assert result["formatted_body"] == sanitize_matrix_html(markdown_to_matrix_html(result["body"]))
    assert result[INTERACTIVE_UI_KEY]["nodes"][0]["html"] == '<p>Rich <b>text</b><a>link</a></p>'
    assert result[INTERACTIVE_UI_KEY]["nodes"][0]["text"] == "Plain text"
    assert raw == original
    result[INTERACTIVE_UI_KEY]["nodes"][0]["text"] = "changed"
    assert raw == original
    assert isinstance(parse_interactive_ui(build(raw)[INTERACTIVE_UI_KEY]), ParsedUi)


@pytest.mark.parametrize("without_markdown", [False, True], ids=["installed", "missing"])
@pytest.mark.parametrize("literal", [
    'Keep onclick="literal" here & <tag> **bold**',
    "Keep ONCLICK = 'literal' and onload=plain here",
    'Keep href="javascript:literal" and src="data:literal" here',
    'Keep **bold** `code` [link](https://example.org/) & &#61; $x$ = here',
    r'Keep &amp; &lt; &#36; &#32; &#x3d; \\ \*escaped\* literal',
    '# fake\n- list\n1. item\n**literal**\n    indented',
    '<b onclick="literal">tag</b><script>bad()</script><img src="javascript:bad()" onerror="bad()">',
])
def test_strict_valid_literal_text_survives_real_html_pipeline(literal, without_markdown, request):
    if without_markdown:
        request.getfixturevalue("missing_markdown")
    raw = payload()
    raw.pop("title")
    raw["nodes"][0]["text"] = literal
    parsed = parse_interactive_ui(raw, strict=True)
    assert isinstance(parsed, ParsedUi), parsed
    result = build(parsed.payload)
    assert isinstance(result, dict), result
    # Decode entities once, preserving literal entity spellings such as &#61;.
    body_text = unescape(re.sub(r"\\([\\`*_{}\[\]()#+.!|~\-])", r"\1", result["body"]))
    assert literal in body_text
    text = []
    tags = []
    reader = HTMLParser()
    reader.handle_data = text.append
    reader.handle_starttag = lambda tag, attrs: tags.append((tag, attrs))
    reader.feed(result["formatted_body"])
    reader.close()
    assert literal == "".join(text)
    assert all(tag in {"p", "br"} and not attrs for tag, attrs in tags)
    assert result[INTERACTIVE_UI_KEY]["nodes"][0]["html"] == '<p>Rich <b>text</b><a>link</a></p>'


@pytest.mark.parametrize("without_markdown", [False, True], ids=["installed", "missing"])
def test_strict_valid_ordered_list_keeps_numbering(without_markdown, request):
    if without_markdown:
        request.getfixturevalue("missing_markdown")
    raw = payload()
    raw.pop("title")
    raw["nodes"] = [{"id": "root", "type": "list", "ordered": True,
                     "items": [{"text": text} for text in ("First", "Second", "Third")]}]
    parsed = parse_interactive_ui(raw, strict=True)
    assert isinstance(parsed, ParsedUi), parsed
    result = build(parsed.payload)
    assert isinstance(result, dict), result
    assert result["body"] == "1. First\n\n2. Second\n\n3. Third"
    html = result["formatted_body"]
    assert html.count("<ol>") == 1
    assert html.count("<li>") == 3
    text = []
    reader = HTMLParser()
    reader.handle_data = lambda data: text.append(data.strip()) if data.strip() else None
    reader.feed(html)
    reader.close()
    assert text == ["First", "Second", "Third"]
    if not without_markdown:
        assert html == sanitize_matrix_html(markdown_to_matrix_html(result["body"]))


def test_missing_markdown_ordered_lists_keep_restarts_and_content_boundaries(missing_markdown):
    raw = payload()
    raw.pop("title")
    raw["nodes"] = [
        {"id": "root", "type": "stack", "direction": "vertical",
         "children": ["first", "restart", "paragraph", "after_text", "bullets", "last"]},
        {"id": "first", "type": "list", "ordered": True, "items": [{"text": "First"}, {"text": "Second"}]},
        {"id": "restart", "type": "list", "ordered": True, "items": [{"text": "Restart"}]},
        {"id": "paragraph", "type": "text", "text": "Between lists"},
        {"id": "after_text", "type": "list", "ordered": True, "items": [{"text": "After paragraph"}]},
        {"id": "bullets", "type": "list", "items": [{"text": "Bullet"}]},
        {"id": "last", "type": "list", "ordered": True, "items": [{"text": "After bullets"}]},
    ]
    parsed = parse_interactive_ui(raw, strict=True)
    assert isinstance(parsed, ParsedUi), parsed
    result = build(parsed.payload)
    assert isinstance(result, dict), result
    assert result["formatted_body"] == (
        "<ol><li>First</li><li>Second</li></ol>\n\n"
        "<ol><li>Restart</li></ol>\n\n<p>Between lists</p>\n\n"
        "<ol><li>After paragraph</li></ol>\n\n<ul><li>Bullet</li></ul>\n\n"
        "<ol><li>After bullets</li></ol>"
    )


def test_custom_converter_keeps_precedence_without_markdown(missing_markdown):
    calls = []

    def convert(text):
        calls.append(text)
        return '<b>Custom output</b><script>bad()</script>'

    result = build(to_html=convert)
    assert calls == [result["body"]]
    assert result["formatted_body"] == "<b>Custom output</b>"


def test_formatted_body_is_sanitized_even_with_custom_converter():
    result = build(to_html=lambda text: '<b>kept</b><img onerror="bad()"><script>bad()</script>')
    assert result["formatted_body"] == "<b>kept</b>"


@pytest.mark.parametrize("metadata", [None, {}, {"thread_id": ""}, {"unrelated": "$not-a-thread"}])
def test_unthreaded_new_events_have_no_relation(metadata):
    assert "m.relates_to" not in build(thread=metadata)


def test_threaded_new_event_matches_adapter_relation_and_copies_metadata():
    metadata = {"thread_id": "$root:server", "other": {"retained": [1]}}
    original = deepcopy(metadata)
    result = build(thread=metadata)
    assert result["m.relates_to"] == {
        "rel_type": "m.thread", "event_id": "$root:server", "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$root:server"},
    }
    assert result["m.mentions"] == {}
    assert metadata == original


@pytest.mark.parametrize("thread", [None, {"thread_id": "$thread"}])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_new_event_exact_utf8_boundary_includes_html_payload_and_relation(thread, offset):
    # A multibyte scalar plus JSON quotes/backslashes defeats character-count estimates.
    prefix = '汉😀"\\\n'
    base = build(fallback=prefix, thread=thread, to_html=lambda text: "<p>HTML 汉😀</p>")
    padding = 44000 - wire_size(base) + offset
    result = build(fallback=prefix + "x" * padding, thread=thread, to_html=lambda text: "<p>HTML 汉😀</p>")
    if offset > 0:
        assert_too_large(result, 44000)
    else:
        assert isinstance(result, dict), result
        assert wire_size(result) == 44000 + offset
        assert result["body"] == prefix + "x" * padding
        assert result["formatted_body"] == "<p>HTML 汉😀</p>"
        assert result[INTERACTIVE_UI_KEY]["nodes"]


def test_full_html_is_never_dropped_or_truncated_to_fit():
    assert_too_large(build(fallback="汉" * 10000, to_html=lambda text: "<p>" + "字" * 5000 + "</p>"), 44000)
    result = build(fallback="complete", to_html=lambda text: "<p>" + "x" * 43000 + "</p>")
    assert isinstance(result, dict)
    assert result["formatted_body"] == "<p>" + "x" * 43000 + "</p>"
    assert result["body"] == "complete"


def test_edit_targets_original_with_full_payload_only_in_new_content():
    new_content = build(thread={"thread_id": "$thread"})
    new_content[INTERACTIVE_UI_KEY]["revision"] = 3
    new_content[INTERACTIVE_UI_KEY]["status"] = "submitted"
    new_content[INTERACTIVE_UI_KEY]["status_text"] = "Submitted"
    original = deepcopy(new_content)
    result = build_ui_edit("$original", new_content, "Submission received")
    assert isinstance(result, dict)
    assert result["body"] == "* Submission received"
    assert result["msgtype"] == "m.text"
    assert result["m.mentions"] == {}
    assert result["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$original"}
    assert INTERACTIVE_UI_KEY not in result
    assert "m.relates_to" not in result["m.new_content"]
    assert result["m.new_content"]["m.mentions"] == {}
    expected = deepcopy(new_content)
    expected.pop("m.relates_to")
    assert result["m.new_content"] == expected
    assert new_content == original
    result["m.new_content"][INTERACTIVE_UI_KEY]["revision"] = 4
    assert new_content == original


def test_edit_uses_title_when_summary_is_empty():
    result = build_ui_edit("$original", build(), "")
    assert result["body"] == "* Complete answer"


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_edit_exact_utf8_boundary_includes_full_wrapper(offset):
    new_content = build(fallback="汉😀", thread={"thread_id": "$thread"})
    base = build_ui_edit("$original", new_content, "Updated 汉😀")
    padded = deepcopy(new_content)
    padded["formatted_body"] += "x" * (45000 - wire_size(base) + offset)
    original = deepcopy(padded)
    result = build_ui_edit("$original", padded, "Updated 汉😀")
    assert padded == original
    if offset > 0:
        assert_too_large(result, 45000)
    else:
        assert isinstance(result, dict), result
        assert wire_size(result) == 45000 + offset
        assert result["m.new_content"]["formatted_body"] == padded["formatted_body"]
        assert result["m.new_content"]["body"] == "汉😀"
        assert result["m.relates_to"]["event_id"] == "$original"


def test_edit_wrapper_growth_is_also_counted():
    new_content = build(fallback="complete", to_html=lambda text: "<p>" + "x" * 43000 + "</p>")
    assert isinstance(new_content, dict)
    assert_too_large(build_ui_edit("$original", new_content, "汉" * 1000), 45000)

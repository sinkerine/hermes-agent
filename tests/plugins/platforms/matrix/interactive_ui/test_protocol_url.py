"""Behavioral URL contracts and conservative Unicode hostname restrictions.

The UTS46 implementation intentionally rejects some hosts conservatively; the
canonical fixtures and client first-error ordering remain unchanged.
"""

import pytest

from plugins.platforms.matrix.interactive_ui.protocol import ParsedUi, parse_interactive_ui
from tests.plugins.platforms.matrix.interactive_ui.test_protocol_conformance import assert_error, basic


@pytest.mark.parametrize("host,accepted", [
    ("-1.1", False), ("+1.1", False), ("1..1", False), ("1_0.1", False),
    ("1.-1.1", False), ("1.+1.1", False), ("0x-1.1", False), ("0x+1.1", False),
    ("0x1_0.1", False), ("01_0.1", False), ("09.1", False), ("0xg.1", False),
    (".1", False), ("256.1", False), ("1.16777216", False), ("4294967296", False),
    ("0x7f.1", True), ("0X7F.1", True), ("0177.1", True), ("127.0.0.1", True),
    ("0x.1", True), ("0X.1", True), ("1.0x", True), ("0x", True), ("00.1", True),
    ("255.16777215", True), ("4294967295", True), ("1.2.3.255", True),
    ("1.2.3.256", False), ("1.2.3.4.5", False), ("1.", True), ("1...", True),
])
@pytest.mark.parametrize("strict", [False, True])
def test_ipv4_url_component_contract(host, accepted, strict):
    raw = basic([{"id": "root", "type": "link", "label": "Link", "url": "https://" + host + "/"}])
    if accepted:
        result = parse_interactive_ui(raw, strict=strict)
        assert isinstance(result, ParsedUi), result
    else:
        assert_error(raw, "nodes[0].url", "bad_url", strict)


@pytest.mark.parametrize("host", ["\u0879\u1012\u7b1e.com", "\u1ad0\u5b0a\uc835.com", "\u8a8f\u228b\ua7ce.com"])
@pytest.mark.parametrize("strict", [False, True])
def test_conservative_unicode_host_rejection(host, strict):
    assert_error(basic([{"id": "root", "type": "link", "label": "Link", "url": "https://" + host}]),
                 "nodes[0].url", "bad_url", strict)

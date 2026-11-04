"""Deterministic producer inputs based on the actual vendored Element contract."""

import base64
from copy import deepcopy
import json

from common import ROOT, SB

VENDOR = ROOT / "plugins/platforms/matrix/interactive_ui/vendor/element-web-bcb0801/fixtures/valid"
FIXTURES = {
    "graph-zh": "knowledge-map.zh.json", "graph-en": "knowledge-map.en.json",
    "table": "table.json", "chart": "chart-bar.json", "form": "form.json",
    "carousel": "product-carousel.json", "invalid": "knowledge-map.en.json",
}
# Tiny valid PNG, created in the sandbox, then uploaded by the real producer (including E2EE).
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+afo4AAAAASUVORK5CYII="


def fixture(case):
    payload = json.loads((VENDOR / FIXTURES[case]).read_text(encoding="utf-8-sig"))
    for key in ("version", "ui_id", "revision", "status"):
        payload.pop(key, None)
    if case == "carousel":
        images = SB / "assets"
        images.mkdir(exist_ok=True)
        carousel = next(node for node in payload["nodes"] if node["type"] == "carousel")
        carousel["items"] = carousel["items"][:3]
        for index, item in enumerate(carousel["items"]):
            path = images / f"sample-{index}.png"
            path.write_bytes(base64.b64decode(PNG))
            item["image"] = {"alt": f"Sandbox sample {index + 1}", "mimetype": "image/png", "upload": str(path)}
    return payload


def validation_payload(ui):
    payload = deepcopy(ui)
    unresolved_media = False
    for node in payload.get("nodes", []):
        images = [node] if node.get("type") == "image" else [item["image"] for item in node.get("items", [])
                                                                  if isinstance(item, dict) and "image" in item]
        for image in images:
            if "upload" in image:
                image.pop("upload")
                image["url"] = "mxc://localhost/validation-placeholder"
                unresolved_media = True
    payload.update(version=1, ui_id="sandbox_eval", revision=0, status="active")
    return payload, unresolved_media

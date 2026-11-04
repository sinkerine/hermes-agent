"""Worktree-only paths and private sandbox I/O (Linux harness)."""

import json
import os
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SB = ROOT / ".hermes-sandbox/matrix-ui"
HOME = SB / "home"
HS = "http://127.0.0.1:18008"
ELEMENT_SHA = "bcb080171cb312feef37e63007c7ca8cc0a8b77f"
SYNAPSE = "matrixdotorg/synapse@sha256:1909bcc07d4180945464712a7f4d4bb6f644c4c1774d026d77cdadc196b91eb8"
UI_KEY = "net.15cm.matrix.interactive_ui"
ACTION_KEY = UI_KEY + ".action"


def guard():
    expected = HOME / "installs/865670c36e681bef/test-environment/gen-a030d2f93353456a9de98107c9975f81/venv/bin/python"
    assert Path.cwd() == ROOT, "Run from worktree root"
    assert os.environ.get("HERMES_HOME") == str(HOME)
    assert os.environ.get("HERMES_RUNTIME_DIR") == str(HOME / "tools")
    assert os.environ.get("HERMES_PYTHON") == str(expected)
    assert Path(sys.executable).absolute() == expected
    assert not (ROOT / ".hermes-sandbox").is_symlink() and not SB.is_symlink()
    assert SB.resolve() == SB and HOME.resolve() == HOME
    os.umask(0o077)
    for name in ("evidence", "run", "tmp", "cache", "assets", "replay", "synapse", "element-src"):
        target = SB / name
        assert not target.is_symlink() and target.resolve().is_relative_to(SB)
        if name in ("evidence", "run", "tmp", "cache"):
            target.mkdir(parents=True, exist_ok=True)


def private_write(path, text):
    path = Path(path)
    assert path.is_relative_to(SB) and path.resolve().is_relative_to(SB)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)  # windows-footgun: ok — Linux-only sandbox


def write_json(path, value):
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def request(url, data=None, token=None, method=None, timeout=45):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    raw = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=raw, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def entrypoint(fn):
    guard()
    try:
        fn()
    except (OSError, ValueError, KeyError, AssertionError) as exc:
        # HTTP errors and provider responses can contain tokens; report only the class.
        print(f"Sandbox command failed ({type(exc).__name__}); inspect private local state.", file=sys.stderr)
        raise SystemExit(1) from None

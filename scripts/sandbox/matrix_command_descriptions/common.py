"""Paths, ownership and private I/O for the disposable Linux browser harness."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SB = ROOT / '.hermes-sandbox/matrix-commands'
HOME = SB / 'home'
HERE = Path(__file__).resolve().parent
HS = 'http://127.0.0.1:18108'
ELEMENT_SHA = 'd698eb223e7dc3030fed2c49330e9ecb98b4ab0d'
ELEMENT_CHECKOUT = Path('/home/sinkerine/dev/element-web')
CHROME = Path('/nix/store/dqzn64123p2qvidnx0wspr0z96rc407a-google-chrome-156.0.8078.79/bin/google-chrome-stable')
SYNAPSE = 'matrixdotorg/synapse@sha256:1909bcc07d4180945464712a7f4d4bb6f644c4c1774d026d77cdadc196b91eb8'
OWNER = hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
LABEL = 'org.hermes.matrix-command-sandbox'
CONTAINER = 'hermes-mcd-' + OWNER
EVENT = 'org.matrix.msc4391.command_description'
BOT = '@hermes:localhost'


def guard():
    assert Path.cwd() == ROOT, 'Run from worktree root'
    assert os.environ.get('SB') == str(SB)
    assert os.environ.get('HERMES_HOME') == str(HOME)
    assert os.environ.get('HERMES_RUNTIME_DIR') == str(HOME / 'tools')
    assert os.environ.get('HERMES_PYTHON') == str(ROOT / '.venv/bin/python')
    assert Path(sys.executable).absolute() == ROOT / '.venv/bin/python'
    assert SB.resolve() == SB and HOME.resolve() == HOME
    os.umask(0o077)
    for name in ('evidence', 'run', 'tmp', 'cache', 'bin'):
        target = SB / name
        assert target.resolve() == target
        target.mkdir(parents=True, exist_ok=True)


def private_write(path, text):
    path = Path(path)
    assert path.is_relative_to(SB) and path.resolve().is_relative_to(SB)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Refuse symlinks and create with private permissions before writing secrets.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        os.fchmod(handle.fileno(), 0o600)  # windows-footgun: ok — Linux sandbox
        handle.write(text)


def write_json(path, value):
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def claim(directory, marker='.harness-owner'):
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / marker
    if target.exists():
        assert target.read_text(encoding='utf-8-sig') == OWNER, 'Foreign ownership'
    else:
        assert not any(directory.iterdir()), 'Refuse to adopt unowned state'
        private_write(target, OWNER)


def request(url, data=None, token=None, method=None, timeout=45):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    raw = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=raw, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def redact(value):
    if isinstance(value, dict):
        return {key: ('[redacted]' if re.search('token|password|secret|recovery', key, re.IGNORECASE) else redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def state_key(command):
    return base64.b64encode(hashlib.sha256((command + BOT).encode()).digest()).decode()


def assert_dump(events, *, empty=False):
    descriptions = [ev for ev in events if ev['type'] == EVENT and ev.get('sender') == BOT]
    assert descriptions, 'No bot state events; missing publication is not successful cleanup'
    names = set()
    for ev in descriptions:
        content = ev['content']
        if not content:
            continue
        assert not empty, 'Descriptions remain after disable'
        command = content['command']
        assert re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', command)
        assert ev['state_key'] == state_key(command)
        assert isinstance(content['parameters'], list)
        for name in [command, *content.get('aliases', [])]:
            assert name not in names, 'Ambiguous command'
            names.add(name)
    return names


def entrypoint(fn):
    guard()
    try:
        fn()
    except (OSError, ValueError, KeyError, AssertionError) as exc:
        print(f'Sandbox command failed ({type(exc).__name__}); inspect private evidence.', file=sys.stderr)
        raise SystemExit(1) from None

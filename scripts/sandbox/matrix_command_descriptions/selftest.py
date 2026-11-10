"""Offline behavior contracts: no browser, Docker, network or child processes."""

import base64
import hashlib
import json
import tempfile
from pathlib import Path

from common import BOT, EVENT, OWNER, SB, assert_dump, claim, private_write, read_json, redact, state_key
from configs import element_config, gateway_config, synapse_config
from element_build import call_resolution, verify_archive
from procs import child_environment, verify_identity
from services import digest_tree
import hermes_yaml as yaml


def rejects(fn):
    try:
        fn()
    except AssertionError:
        return
    raise AssertionError('Invalid operation was accepted')


def selftest():
    raw = b'exact dependency archive fixture'
    integrity = 'sha512-' + base64.b64encode(hashlib.sha512(raw).digest()).decode()
    verify_archive(raw, integrity)
    rejects(lambda: verify_archive(raw + b'changed', integrity))
    resolution = {'tarball': 'https://codeload.github.com/element-hq/element-call/tar.gz/' + 'a' * 40,
                  'path': '/component', 'integrity': integrity}
    lock = {'packages': {'@element-hq/element-call-component@pinned': {'resolution': resolution}}}
    assert call_resolution(yaml.safe_dump(lock)) == resolution
    assert call_resolution('---\npackages: {}\n---\n' + yaml.safe_dump(lock)) == resolution
    rejects(lambda: call_resolution('packages: {}'))
    rejects(lambda: call_resolution(yaml.safe_dump({'packages': {'@element-hq/element-call-component@bad': {
        'resolution': {**resolution, 'tarball': 'https://example.org/untrusted'}}}})))
    environment = child_environment()
    assert environment['SB'] == str(SB)
    assert 'OPENAI_API_KEY' not in environment and 'MATRIX_ACCESS_TOKEN' not in environment
    config = yaml.safe_load(yaml.safe_dump(gateway_config('!ops:localhost')))
    assert config['matrix']['allowed_users'] == ['@alice:localhost']
    assert config['matrix']['allowed_rooms'] == ['!ops:localhost']
    assert config['matrix']['command_descriptions'] is True
    assert gateway_config('!ops:localhost', True)['matrix']['command_descriptions'] is False
    assert config['model']['base_url'] == 'http://127.0.0.1:18182/v1'
    assert element_config()['features']['feature_user_status'] is True
    synapse = synapse_config({'server_name': 'localhost'})
    assert synapse['rc_message'] == {'per_second': 4, 'burst_count': 30}
    assert synapse['listeners'][0]['resources'][0]['names'] == ['client']
    assert synapse['enable_registration'] is False
    key = state_key('status')
    assert len(key) == 44 and key.endswith('=')
    assert base64.b64decode(key) == hashlib.sha256(('status' + BOT).encode()).digest()
    event = {'type': EVENT, 'sender': BOT, 'state_key': key,
             'content': {'command': 'status', 'parameters': []}}
    fixture = json.loads(json.dumps([event]))
    assert assert_dump(fixture) == {'status'}
    rejects(lambda: assert_dump(fixture, empty=True))
    rejects(lambda: assert_dump([]))
    rejects(lambda: assert_dump([event, event]))
    bad = {**event, 'state_key': 'wrong'}
    rejects(lambda: assert_dump([bad]))
    assert not assert_dump([{**event, 'content': {}}], empty=True)
    record = {'owner': OWNER, 'pid': 123, 'starttime': '45'}
    state = {'starttime': '45', 'group': 123, 'session': 123}
    verify_identity(record, OWNER, state)
    rejects(lambda: verify_identity(record, 'foreign', state))
    rejects(lambda: verify_identity(record, OWNER, {**state, 'starttime': '46'}))
    rejects(lambda: verify_identity(record, OWNER, {**state, 'session': 456}))
    assert redact({'access_token': 'secret', 'nested': [{'password': 'hidden'}], 'count': 1}) == {
        'access_token': '[redacted]', 'nested': [{'password': '[redacted]'}], 'count': 1}
    with tempfile.TemporaryDirectory(dir=SB / 'tmp') as directory:
        root = Path(directory)
        claim(root)
        claim(root)
        private_write(root / '.harness-owner', 'foreign')
        rejects(lambda: claim(root))
        unowned = root / 'unowned'
        unowned.mkdir()
        private_write(unowned / 'existing', 'data')
        rejects(lambda: claim(unowned))
        secret = root / 'accounts.json'
        private_write(secret, '{"password":"private"}')
        assert secret.stat().st_mode & 0o777 == 0o600
        assert read_json(secret)['password'] == 'private'
        first = digest_tree(unowned)
        private_write(unowned / 'existing', 'changed')
        assert digest_tree(unowned) != first
    print('selftest: PASS — configs, state keys, dump assertions, unowned-state refusal, receipt identity, private I/O, redaction, artifact digest, pinned dependency integrity')

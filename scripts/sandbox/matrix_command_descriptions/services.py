"""Owned processes, loopback services and immutable Element build provenance."""

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

from common import (CHROME, CONTAINER, ELEMENT_CHECKOUT, ELEMENT_SHA, HERE, HS, LABEL,
                    OWNER, ROOT, SB, SYNAPSE, claim, private_write, read_json, request, write_json)
from configs import bootstrap, configure_gateway, element_config, synapse_config
from procs import child_environment, launch, stop_receipt
import hermes_yaml as yaml


def run(args, **kwargs):
    receipt = SB / 'run' / ('command-' + uuid.uuid4().hex + '.json')
    timeout = kwargs.pop('timeout', 60)
    child = launch(args, receipt, OWNER, **kwargs)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
        if child.returncode:
            raise subprocess.CalledProcessError(child.returncode, args, stdout, stderr)
        return stdout
    finally:
        stop_receipt(receipt, OWNER)
        child.wait(timeout=5)


def start(name, args):
    with (SB / 'evidence' / (name + '.log')).open('ab') as output:
        launch(args, SB / 'run' / (name + '.json'), OWNER, stdout=output, stderr=output)


def stop(name):
    stop_receipt(SB / 'run' / (name + '.json'), OWNER)


def preflight():
    checks = {name: bool(shutil.which(name)) for name in ('docker', 'node', 'corepack', 'git', 'tar')}
    checks['chrome'] = CHROME.is_file()
    for name in ('mautrix.crypto', 'olm', 'asyncpg', 'aiosqlite'):
        try:
            checks[name] = importlib.util.find_spec(name) is not None
        except ModuleNotFoundError:
            checks[name] = False
    for port in (18108, 18181, 18182):
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
                checks[str(port)] = True
            except OSError:
                checks[str(port)] = False
    for key, args in (
        ('docker_daemon', ['docker', 'info']),
        ('synapse_image', ['docker', 'image', 'inspect', SYNAPSE]),
        ('element_commit', ['git', '-C', str(ELEMENT_CHECKOUT), 'cat-file', '-e', ELEMENT_SHA + '^{commit}']),
    ):
        checks[key] = bool(shutil.which(args[0])) and subprocess.run(
            args, capture_output=True, timeout=20, check=False, env=child_environment()).returncode == 0
    write_json(SB / 'evidence/preflight.json', checks)
    for key, value in checks.items():
        print(f'{key}: {"available" if value else "BLOCKED"}')
    assert all(checks.values()), 'Resolve preflight blockers'


def digest_tree(path):
    digest = hashlib.sha256()
    for entry in sorted(path.rglob('*')):
        if entry.is_file():
            digest.update(entry.relative_to(path).as_posix().encode() + b'\0')
            digest.update(hashlib.sha256(entry.read_bytes()).digest())
    return digest.hexdigest()


def provenance():
    source = SB / 'element-src'
    assert (source / '.sandbox-commit').read_text(encoding='utf-8-sig') == ELEMENT_SHA
    assert (source / 'apps/web/webapp/index.html').is_file()
    return {'commit': ELEMENT_SHA, 'lock_sha256': hashlib.sha256((source / 'pnpm-lock.yaml').read_bytes()).hexdigest(),
            'webapp_sha256': digest_tree(source / 'apps/web/webapp')}


def verify_element():
    assert provenance() == read_json(SB / 'evidence/element-build-provenance.json'), 'Element artifact changed'
    print('Element commit, lock and webapp digests verified.')


def build_element():
    source = SB / 'element-src'
    assert not source.exists(), 'Refuse to overwrite Element archive'
    source.mkdir()
    archive = SB / 'tmp/element.tar'
    with archive.open('wb') as output:
        run(['git', '-C', str(ELEMENT_CHECKOUT), 'archive', ELEMENT_SHA], stdout=output)
    run(['tar', '-xf', str(archive), '-C', str(source)])
    archive.unlink()
    with (SB / 'evidence/element-build.log').open('ab') as output:
        run(['corepack', 'install', '-g', 'pnpm@12.11.2'], stdout=output, stderr=output, timeout=300)
        private_write(SB / 'bin/pnpm', '#!/usr/bin/env bash\nexec corepack pnpm "$@"\n')
        (SB / 'bin/pnpm').chmod(0o700)  # windows-footgun: ok — Linux shell shim
        commands = [
            ['pnpm', 'install', '--frozen-lockfile', '--ignore-scripts', '--store-dir', str(SB / 'cache/pnpm-store')],
            ['pnpm', '--filter', 'element-web', 'rebuild', '@element-hq/element-call-component', 'matrix-js-sdk'],
            ['pnpm', '--config.verifyDepsBeforeRun=false', '--filter', 'element-web', 'run', 'build'],
        ]
        for command in commands:
            run(command, cwd=source, stdout=output, stderr=output, timeout=1800)
    private_write(source / '.sandbox-commit', ELEMENT_SHA)
    write_json(source / 'apps/web/webapp/config.json', element_config())
    write_json(SB / 'evidence/element-build-provenance.json', provenance())


def owned_container():
    result = subprocess.run(['docker', 'inspect', '--format', '{{index .Config.Labels "' + LABEL + '"}}', CONTAINER],
                            capture_output=True, text=True, encoding='utf-8', timeout=15, check=False,
                            env=child_environment())
    if result.returncode:
        return False
    assert result.stdout.strip() == OWNER, 'Refuse foreign container'
    return True


def wait_http(url, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return request(url)
        except OSError:
            time.sleep(0.2)
    raise ValueError('Loopback service readiness timeout')


def synapse():
    assert not owned_container(), 'Stop previous container'
    data = SB / 'synapse'
    claim(data)
    args = ['docker', 'run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}',  # windows-footgun: ok — Linux harness
            '--label', f'{LABEL}={OWNER}', '-v', f'{data}:/data']
    config_path = data / 'homeserver.yaml'
    if not config_path.exists():
        with (SB / 'evidence/synapse-generate.log').open('ab') as output:
            run([*args, '-e', 'SYNAPSE_SERVER_NAME=localhost', '-e', 'SYNAPSE_REPORT_STATS=no', SYNAPSE, 'generate'],
                stdout=output, stderr=output)
        config = yaml.safe_load(config_path.read_text(encoding='utf-8-sig'))
        private_write(config_path, yaml.safe_dump(synapse_config(config)))
    run([*args, '-d', '--name', CONTAINER, '-p', '127.0.0.1:18108:8008', SYNAPSE], stdout=subprocess.PIPE)
    wait_http(HS + '/_matrix/client/versions')


def up():
    preflight()
    verify_element()
    # Only this marker claims the runtime; offline selftest never creates it.
    marker = SB / '.runtime-owner'
    if marker.exists():
        assert marker.read_text(encoding='utf-8-sig') == OWNER
    else:
        assert not any((SB / name).exists() for name in ('accounts.json', 'rooms.json', 'home', 'synapse'))
        private_write(marker, OWNER)
    assert not list((SB / 'run').glob('*.json')), 'Stop previous run'
    try:
        synapse()
        bootstrap()
        start('provider', [sys.executable, str(HERE / 'provider_stub.py')])
        wait_http('http://127.0.0.1:18182/v1/models')
        start('element', [sys.executable, '-m', 'http.server', '18181', '--bind', '127.0.0.1',
                          '--directory', str(SB / 'element-src/apps/web/webapp')])
        restart_gateway()
        write_json(SB / 'evidence/provenance.json', {'owner': OWNER, 'element': provenance(),
            'synapse': SYNAPSE, 'gateway_commit': run(['git', 'rev-parse', 'HEAD'], stdout=subprocess.PIPE).decode().strip()})
    except BaseException:
        down(False)
        raise


def restart_gateway(disable=False):
    assert (SB / '.runtime-owner').read_text(encoding='utf-8-sig') == OWNER
    stop('gateway')
    configure_gateway(disable)
    start('gateway', [sys.executable, '-m', 'gateway.run'])


def down(purge=False):
    marker = SB / '.runtime-owner'
    assert marker.read_text(encoding='utf-8-sig') == OWNER, 'Refuse unowned runtime'
    for receipt in sorted((SB / 'run').glob('*.json')):
        stop_receipt(receipt, OWNER)
    if owned_container():
        with (SB / 'evidence/synapse.log').open('ab') as output:
            run(['docker', 'logs', CONTAINER], stdout=output, stderr=output)
        run(['docker', 'stop', '--time', '15', CONTAINER], stdout=subprocess.PIPE)
    if purge:
        for name in ('synapse', 'element-src', 'home', 'browser', 'accounts.json', 'rooms.json', 'bot-recovery-key.txt'):
            path = SB / name
            assert path.resolve() == path
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        marker.unlink()
    print('Owned services stopped; evidence retained.')

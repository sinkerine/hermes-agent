"""Build exact Git dependency sources omitted by pnpm's ignored prepare hooks."""

import base64
import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile

from common import ELEMENT_CHECKOUT, ELEMENT_SHA, SB, private_write, read_json, write_json
from ruamel.yaml import YAML


def call_resolution(lock_text):
    # pnpm 12 separates the package-manager and application locks into YAML documents.
    documents = YAML(typ='safe', pure=True).load_all(lock_text)
    entries = [value for lock in documents for key, value in lock.get('packages', {}).items()
               if key.startswith('@element-hq/element-call-component@')]
    assert len(entries) == 1, 'Expected one pinned Element Call dependency'
    resolution = entries[0]['resolution']
    assert re.fullmatch(r'https://codeload.github.com/element-hq/element-call/tar.gz/[0-9a-f]{40}',
                        resolution['tarball'])
    assert resolution['path'] == '/component'
    assert resolution['integrity'].startswith('sha512-')
    return resolution


def verify_archive(raw, integrity):
    actual = 'sha512-' + base64.b64encode(hashlib.sha512(raw).digest()).decode()
    assert actual == integrity, 'Git dependency archive integrity mismatch'


def guard_source(source, run):
    assert source.resolve() == source
    assert not (SB / 'evidence/element-build-provenance.json').exists(), 'Refuse a completed build'
    for name in ('pnpm-lock.yaml', 'package.json', 'pnpm-workspace.yaml'):
        expected = run(['git', '-C', str(ELEMENT_CHECKOUT), 'show', f'{ELEMENT_SHA}:{name}'],
                       stdout=subprocess.PIPE)
        assert (source / name).read_bytes() == expected, f'Pinned Element input changed: {name}'
    marker = source / '.sandbox-commit'
    if marker.exists():
        assert marker.read_text(encoding='utf-8-sig') == ELEMENT_SHA


def prepare_call(source, run, output):
    from services import digest_tree

    resolution = call_resolution((source / 'pnpm-lock.yaml').read_text(encoding='utf-8-sig'))
    revision = resolution['tarball'].rsplit('/', 1)[1]
    destination = (source / 'apps/web/node_modules/@element-hq/element-call-component').resolve()
    assert destination.is_relative_to(source / 'node_modules/.pnpm')
    receipt = SB / 'evidence/element-call-build.json'
    if (destination / 'dist').exists():
        prepared = read_json(receipt)
        assert prepared['revision'] == revision and prepared['integrity'] == resolution['integrity']
        assert prepared['dist_sha256'] == digest_tree(destination / 'dist'), 'Prepared dependency changed'
        return prepared
    dependency = SB / ('element-call-' + revision)
    archive = SB / 'tmp' / ('element-call-' + revision + '.tar.gz')
    if not archive.exists():
        run(['curl', '--fail', '--location', '--max-time', '120', resolution['tarball'], '--output', str(archive)],
            stdout=output, stderr=output, timeout=150)
    raw = archive.read_bytes()
    verify_archive(raw, resolution['integrity'])
    if not dependency.exists():
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            tar.extractall(SB, filter='data')
        private_write(dependency / '.sandbox-integrity', resolution['integrity'])
    assert (dependency / '.sandbox-integrity').read_text(encoding='utf-8-sig') == resolution['integrity']
    dependency_lock = (dependency / 'pnpm-lock.yaml').read_bytes()
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        expected = tar.extractfile(f'element-call-{revision}/pnpm-lock.yaml').read()
    assert dependency_lock == expected, 'Pinned Element Call lock changed'
    # Its own repository has no root postinstall; use its frozen install and declared build.
    run(['env', 'MCD_CALL_BUILD=1', 'pnpm', 'install', '--frozen-lockfile',
         '--store-dir', str(SB / 'cache/pnpm-store')],
        cwd=dependency, stdout=output, stderr=output, timeout=1800)
    run(['env', 'MCD_CALL_BUILD=1', 'pnpm', '--config.verifyDepsBeforeRun=false', 'run', 'build:component'],
        cwd=dependency, stdout=output, stderr=output, timeout=1800)
    assert (dependency / 'pnpm-lock.yaml').read_bytes() == dependency_lock
    assert read_json(destination / 'package.json')['name'] == '@element-hq/element-call-component'
    assert not (destination / 'dist').exists(), 'Refuse to overwrite prepared dependency'
    shutil.copytree(dependency / 'component/dist', destination / 'dist')
    sdk = {key: value['resolution'] for document in YAML(typ='safe', pure=True).load_all(dependency_lock)
           for key, value in document.get('packages', {}).items()
           if key.startswith('matrix-js-sdk@') and value.get('resolution', {}).get('gitHosted')}
    prepared = {'revision': revision, 'integrity': resolution['integrity'],
                'matrix_js_sdk': sdk, 'pnpm': '12.11.2',
                'call_build_environment': {'MCD_CALL_BUILD': '1'},
                'call_pnpm_override': '--pm-on-fail=ignore',
                'lock_sha256': hashlib.sha256(dependency_lock).hexdigest(),
                'dist_sha256': digest_tree(destination / 'dist')}
    write_json(receipt, prepared)
    return prepared


def build_element(resume=False):
    from configs import element_config
    from services import provenance, run

    source = SB / 'element-src'
    if resume:
        assert source.is_dir(), 'No partial archive to resume'
        guard_source(source, run)
    else:
        assert not source.exists(), 'Use --resume only for an unfinished pinned archive'
        source.mkdir()
        archive = SB / 'tmp/element.tar'
        with archive.open('wb') as output:
            run(['git', '-C', str(ELEMENT_CHECKOUT), 'archive', ELEMENT_SHA], stdout=output)
        run(['tar', '-xf', str(archive), '-C', str(source)])
        guard_source(source, run)
    with (SB / 'evidence/element-build.log').open('ab') as output:
        run(['corepack', 'install', '-g', 'pnpm@12.11.2'], stdout=output, stderr=output, timeout=300)
        private_write(SB / 'bin/pnpm', '#!/usr/bin/env bash\n'
                      'if [ "${MCD_CALL_BUILD:-}" = 1 ]; then\n'
                      '  echo "MCD Call pnpm: --pm-on-fail=ignore" >&2\n'
                      '  exec corepack pnpm --pm-on-fail=ignore "$@"\n'
                      'fi\nexec corepack pnpm "$@"\n')
        (SB / 'bin/pnpm').chmod(0o700)  # windows-footgun: ok — Linux shell shim
        tools = {name: {'path': os.path.realpath(shutil.which(name)),
                        'version': run([name, '--version'], stdout=subprocess.PIPE).decode().strip()}
                 for name in ('node', 'corepack', 'pnpm', 'curl')}
        write_json(SB / 'evidence/element-build-tools.json', tools)
        run(['pnpm', 'install', '--frozen-lockfile', '--ignore-scripts', '--store-dir', str(SB / 'cache/pnpm-store')],
            cwd=source, stdout=output, stderr=output, timeout=1800)
        prepared = prepare_call(source, run, output)
        run(['pnpm', '--filter', 'element-web', 'rebuild', 'matrix-js-sdk'],
            cwd=source, stdout=output, stderr=output, timeout=1800)
        run(['pnpm', '--config.verifyDepsBeforeRun=false', '--filter', 'element-web', 'run', 'build'],
            cwd=source, stdout=output, stderr=output, timeout=1800)
    guard_source(source, run)
    private_write(source / '.sandbox-commit', ELEMENT_SHA)
    write_json(source / 'apps/web/webapp/config.json', element_config())
    write_json(SB / 'evidence/element-call-build.json', prepared)
    write_json(SB / 'evidence/element-build-provenance.json', provenance())

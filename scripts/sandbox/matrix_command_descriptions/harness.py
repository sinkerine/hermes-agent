"""CLI for the isolated MSC4391 Synapse/Element browser journey."""

import argparse
import re
import subprocess
import sys
import time
from urllib.parse import quote
import uuid

from common import (BOT, EVENT, HERE, HOME, SB, assert_dump, entrypoint, private_write,
                    read_json, request, write_json)
from configs import api, room_id, state
from element_build import build_element
from services import down, preflight, restart_gateway, run, up, verify_element

STEPS = ('login-and-dm', 'autocomplete-status', 'run-status', 'ops-empty', 'ops-granted',
         'reload-added', 'reload-removed', 'disabled')


def dump_state(room, label):
    assert re.fullmatch(r'[a-z0-9-]+', label), 'Unsafe evidence label'
    events = state(room)
    write_json(SB / 'evidence' / (label + '.json'), events)
    print(f'{label}: {len(events)} state events saved')


def set_bot_power(room, level):
    room = room_id(room)
    levels = next(ev['content'] for ev in state(room) if ev['type'] == 'm.room.power_levels')
    levels.setdefault('users', {})[BOT] = level
    api('alice', '/rooms/' + quote(room, safe='') + '/state/m.room.power_levels/', levels, 'PUT')


def bob_command(room, text):
    room = room_id(room)
    path = '/rooms/' + quote(room, safe='')
    before = api('alice', path + '/messages?dir=b&limit=1')['start']
    content = {'msgtype': 'm.text', 'body': text, 'm.mentions': {'user_ids': [BOT]},
               'org.matrix.msc4391.command': {'command': text.lstrip('/'), 'arguments': {}}}
    sent = api('bob', path + '/send/m.room.message/' + uuid.uuid4().hex, content, 'PUT')
    deadline = time.monotonic() + 20
    seen = []
    while time.monotonic() < deadline:
        messages = api('alice', path + '/messages?dir=f&limit=100&from=' + quote(before, safe=''))
        seen.extend(messages['chunk'])
        before = messages.get('end', before)
        assert not any(ev.get('sender') == BOT and ev['type'] in ('m.room.message', 'm.room.encrypted') for ev in seen)
        assert request('http://127.0.0.1:18182/counts')['completions'] == 0
        time.sleep(0.5)
    write_json(SB / 'evidence/bob-denied.json', {'sent_event_id': sent['event_id'],
               'observation_seconds': 20, 'bot_replies': 0, 'completions': 0})


def skill(action):
    path = HOME / 'skills/mcd-probe/SKILL.md'
    if action == 'add':
        assert not path.exists(), 'Probe already exists'
        private_write(path, '---\nname: mcd-probe\ndescription: Matrix command publication probe\n---\nReturn the word probe.\n')
    else:
        assert path.resolve().is_relative_to(HOME) and path.is_file()
        path.unlink()
    print('Probe changed; send /reload-skills in the browser next.')


def browser(step):
    verify_element()
    with (SB / 'evidence/browser.log').open('ab') as output:
        run(['node', str(HERE / 'browser_journey.mjs'), step, str(SB)],
            cwd=SB / 'tmp', stdout=output, stderr=output, timeout=180)


def conformance():
    verify_element()
    labels = ['dm-initial', 'ops-granted', 'dm-removed']
    if (SB / 'evidence/disabled.json').exists():
        labels.extend(['disabled', 'ops-disabled'])
    for label in labels:
        events = read_json(SB / 'evidence' / (label + '.json'))
        names = assert_dump(events, empty=label in ('disabled', 'ops-disabled'))
        if label not in ('disabled', 'ops-disabled'):
            assert 'status' in names
    source = SB / 'element-src'
    target = source / 'apps/web/src/slash-commands/bot-commands/HermesPublication.test.ts'
    private_write(target, (HERE / 'element/HermesPublication.test.ts').read_text(encoding='utf-8-sig'))
    # Test input contains real state dumps only, never a generated manifest.
    write_json(target.with_name('hermes-publication-dumps.json'), {
        label: read_json(SB / 'evidence' / (label + '.json')) for label in labels})
    with (SB / 'evidence/conformance.log').open('ab') as output:
        run(['pnpm', '--config.verifyDepsBeforeRun=false', 'exec', 'vitest', 'run',
             'apps/web/src/slash-commands/bot-commands/HermesPublication.test.ts'],
            cwd=source, stdout=output, stderr=output, timeout=180)
    verify_element()
    print('Actual published dumps passed pinned Element parser/resolver/content assertions.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('preflight', 'verify-element', 'up', 'conformance', 'selftest'):
        commands.add_parser(name)
    build = commands.add_parser('build-element')
    build.add_argument('--resume', action='store_true')
    restart = commands.add_parser('restart-gateway')
    restart.add_argument('--disable', action='store_true')
    shutdown = commands.add_parser('down')
    shutdown.add_argument('--purge', action='store_true')
    dump = commands.add_parser('dump-state')
    dump.add_argument('--room', required=True)
    dump.add_argument('--label', required=True)
    power = commands.add_parser('set-bot-power')
    power.add_argument('--room', required=True)
    power.add_argument('--level', required=True, type=int)
    bob = commands.add_parser('bob-command')
    bob.add_argument('--room', required=True)
    bob.add_argument('--text', required=True)
    probe = commands.add_parser('skill')
    probe.add_argument('action', choices=('add', 'remove'))
    web = commands.add_parser('browser')
    web.add_argument('--step', required=True, choices=STEPS)
    args = parser.parse_args()
    actions = {'preflight': preflight, 'verify-element': verify_element,
               'up': up, 'conformance': conformance}
    if args.command in actions:
        actions[args.command]()
    elif args.command == 'selftest':
        from selftest import selftest
        selftest()
    elif args.command == 'build-element':
        build_element(args.resume)
    elif args.command == 'restart-gateway':
        restart_gateway(args.disable)
    elif args.command == 'down':
        down(args.purge)
    elif args.command == 'dump-state':
        dump_state(args.room, args.label)
    elif args.command == 'set-bot-power':
        set_bot_power(args.room, args.level)
    elif args.command == 'bob-command':
        bob_command(args.room, args.text)
    elif args.command == 'skill':
        skill(args.action)
    elif args.command == 'browser':
        browser(args.step)


if __name__ == '__main__':
    entrypoint(main)

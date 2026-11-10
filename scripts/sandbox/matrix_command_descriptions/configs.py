"""Pure configuration builders and sandbox-only Matrix bootstrap."""

import hashlib
import hmac
import secrets
from urllib.parse import quote

from common import BOT, HOME, HS, SB, private_write, read_json, request, write_json
import hermes_yaml as yaml


def synapse_config(config):
    return {**config, 'enable_registration': False, 'report_stats': False,
            'registration_shared_secret': secrets.token_hex(32), 'public_baseurl': HS + '/',
            'signing_key_path': '/data/localhost.signing.key',
            'rc_message': {'per_second': 4, 'burst_count': 30},
            'listeners': [{'port': 8008, 'tls': False, 'type': 'http', 'x_forwarded': True,
                           'resources': [{'names': ['client'], 'compress': False}]}]}


def element_config():
    return {'default_server_config': {'m.homeserver': {'base_url': HS, 'server_name': 'localhost'}},
            'disable_custom_urls': True, 'show_labs_settings': True,
            'features': {'feature_user_status': True}, 'brand': 'Hermes command sandbox'}


def gateway_config(ops, disable=False):
    return {'model': {'provider': 'custom', 'base_url': 'http://127.0.0.1:18182/v1',
                      'default': 'sandbox', 'streaming': False, 'context_length': 128000},
            'matrix': {'allowed_users': ['@alice:localhost'], 'allowed_rooms': [ops],
                       'require_mention': False, 'thread_require_mention': False,
                       'command_descriptions': not disable},
            'memory': {'memory_enabled': False, 'user_profile_enabled': False}}


def api(user, path, data=None, method=None):
    accounts = read_json(SB / 'accounts.json')
    return request(HS + '/_matrix/client/v3' + path, data, accounts[user]['access_token'], method)


def room_id(name):
    rooms = read_json(SB / 'rooms.json')
    return rooms.get(name, name)


def state(room):
    return api('alice', '/rooms/' + quote(room_id(room), safe='') + '/state')


def bootstrap():
    path = SB / 'accounts.json'
    accounts = read_json(path) if path.exists() else {}
    secret = yaml.safe_load((SB / 'synapse/homeserver.yaml').read_text(encoding='utf-8-sig'))['registration_shared_secret']
    for user in ('hermes', 'alice', 'bob'):
        if user not in accounts:
            password = secrets.token_urlsafe(32)
            nonce = request(HS + '/_synapse/admin/v1/register')['nonce']
            mac = hmac.new(secret.encode(), '\0'.join((nonce, user, password, 'notadmin')).encode(), hashlib.sha1).hexdigest()
            request(HS + '/_synapse/admin/v1/register', {'nonce': nonce, 'username': user,
                    'password': password, 'admin': False, 'mac': mac})
            accounts[user] = {'user_id': f'@{user}:localhost', 'password': password}
            write_json(path, accounts)
        if 'access_token' not in accounts[user]:
            login = request(HS + '/_matrix/client/v3/login', {'type': 'm.login.password',
                'identifier': {'type': 'm.id.user', 'user': accounts[user]['user_id']},
                'password': accounts[user]['password'], 'device_id': 'HERMES-MCD-' + user.upper()})
            accounts[user].update(access_token=login['access_token'], device_id=login['device_id'])
            write_json(path, accounts)
    api('hermes', '/profile/' + quote(BOT, safe='') + '/displayname', {'displayname': 'hermes'}, 'PUT')
    rooms_path = SB / 'rooms.json'
    rooms = read_json(rooms_path) if rooms_path.exists() else {}
    for name, power in (('ops', 0), ('elsewhere', 100)):
        if name in rooms:
            continue
        room = api('alice', '/createRoom', {'name': name, 'preset': 'private_chat',
            'invite': [BOT, '@bob:localhost'], 'power_level_content_override': {
                'users': {BOT: power}, 'state_default': 50}})['room_id']
        for user in ('hermes', 'bob'):
            api(user, '/join/' + quote(room, safe=''), {})
        rooms[name] = room
        write_json(rooms_path, rooms)
    bot = accounts['hermes']
    env = {'MATRIX_HOMESERVER': HS, 'MATRIX_ACCESS_TOKEN': bot['access_token'],
           'MATRIX_USER_ID': BOT, 'MATRIX_DEVICE_ID': bot['device_id'], 'MATRIX_E2EE_MODE': 'required',
           'MATRIX_RECOVERY_KEY_OUTPUT_FILE': str(SB / 'bot-recovery-key.txt'),
           'OPENAI_API_KEY': 'sandbox-local-only', 'OPENAI_BASE_URL': 'http://127.0.0.1:18182/v1'}
    private_write(HOME / '.env', ''.join(f'{key}="{value}"\n' for key, value in env.items()))
    configure_gateway()


def configure_gateway(disable=False):
    private_write(HOME / 'config.yaml', yaml.safe_dump(gateway_config(room_id('ops'), disable)))

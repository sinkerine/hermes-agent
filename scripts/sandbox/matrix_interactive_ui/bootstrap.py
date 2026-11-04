"""Register disposable local accounts and write only sandbox gateway configuration."""

import argparse
import hashlib
import hmac
import secrets


from common import HOME, HS, ROOT, SB, entrypoint, private_write, read_json, request, write_json
import hermes_yaml as yaml


def register(username, password, secret):
    nonce = request(HS + "/_synapse/admin/v1/register")["nonce"]
    message = "\0".join((nonce, username, password, "notadmin")).encode()
    mac = hmac.new(secret.encode(), message, hashlib.sha1).hexdigest()
    request(HS + "/_synapse/admin/v1/register", {
        "nonce": nonce, "username": username, "password": password,
        "admin": False, "mac": mac,
    })


def configure():
    owner = hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    assert (SB / ".harness-owner").read_text(encoding="utf-8-sig") == owner
    accounts_path = SB / "accounts.json"
    if accounts_path.exists():
        accounts = read_json(accounts_path)
    else:
        secret = yaml.safe_load((SB / "synapse/homeserver.yaml").read_text(encoding="utf-8-sig"))["registration_shared_secret"]
        accounts = {}
        # Save each registration receipt immediately so retries never reset an existing password.
        for username in ("hermes-bot", "alice", "bob"):
            password = secrets.token_urlsafe(32)
            register(username, password, secret)
            accounts[username] = {"user_id": f"@{username}:localhost", "password": password}
            write_json(accounts_path, accounts)
    assert set(accounts) == {"hermes-bot", "alice", "bob"}, "Partial bootstrap: purge before retry"
    bot = accounts["hermes-bot"]
    if "access_token" not in bot:
        login = request(HS + "/_matrix/client/v3/login", {
            "type": "m.login.password", "identifier": {"type": "m.id.user", "user": bot["user_id"]},
            "password": bot["password"], "initial_device_display_name": "Hermes sandbox bot",
        })
        bot.update(access_token=login["access_token"], device_id=login["device_id"])
        write_json(accounts_path, accounts)
    env = {
        "MATRIX_HOMESERVER": HS, "MATRIX_ACCESS_TOKEN": bot["access_token"],
        "MATRIX_USER_ID": bot["user_id"], "MATRIX_DEVICE_ID": bot["device_id"],
        "MATRIX_E2EE_MODE": "required", "MATRIX_ALLOWED_USERS": "@alice:localhost,@bob:localhost",
        "MATRIX_RECOVERY_KEY_OUTPUT_FILE": str(SB / "bot-recovery-key.txt"),
        "OPENAI_API_KEY": "sandbox-local-only", "OPENAI_BASE_URL": "http://127.0.0.1:18082/v1",
    }
    # Values are generated without newlines or quotes; dotenv must not expand shell text.
    private_write(HOME / ".env", "".join(f'{key}="{value}"\n' for key, value in env.items()))
    config = {
        "model": {"provider": "custom", "base_url": env["OPENAI_BASE_URL"], "default": "gpt-6.1-sol",
                  "streaming": False, "context_length": 128000},
        "platform_toolsets": {"matrix": ["interactive_ui"]},
        "matrix": {"allowed_users": ["@alice:localhost", "@bob:localhost"],
                   "require_mention": False, "thread_require_mention": False},
        "agent": {"max_turns": 6}, "memory": {"memory_enabled": False, "user_profile_enabled": False},
    }
    private_write(HOME / "config.yaml", yaml.safe_dump(config, allow_unicode=True))
    print("Disposable accounts and gateway config ready (secrets withheld).")


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    configure()


if __name__ == "__main__":
    entrypoint(main)

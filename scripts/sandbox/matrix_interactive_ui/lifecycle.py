"""Owned disposable Synapse, provider, Element and gateway processes."""

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid


from common import ELEMENT_SHA, HOME, HS, ROOT, SB, SYNAPSE, entrypoint, private_write, read_json, request, write_json
import hermes_yaml as yaml
from processes import child_environment, launch, stop_receipt
from fake_openai import live_request_limit

LABEL = "org.hermes.matrix-ui-sandbox"
OWNER = hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
CONTAINER = "hermes-mui-" + OWNER
HERE = Path(__file__).resolve().parent


def run(args, **kwargs):
    timeout = kwargs.pop("timeout", 60)
    capture = kwargs.pop("capture_output", False)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    receipt = SB / "run" / ("command-" + uuid.uuid4().hex + ".json")
    child = launch(args, receipt, OWNER, **kwargs)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
        if child.returncode:
            raise subprocess.CalledProcessError(child.returncode, args, stdout, stderr)
        return subprocess.CompletedProcess(args, child.returncode, stdout, stderr)
    finally:
        stop_receipt(receipt, OWNER)
        child.wait(timeout=5)


def preflight(build=False):
    checks = {}
    for executable in ("docker", "node", "pnpm", "git", "tar", "ss"):
        checks[executable] = bool(shutil.which(executable))
    for module in ("mautrix.crypto", "olm", "asyncpg", "aiosqlite", "hermes_yaml"):
        try:
            checks[module] = importlib.util.find_spec(module) is not None
        except ModuleNotFoundError:
            checks[module] = False
    for port in (18008, 18081, 18082):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                checks[f"port_{port}_free"] = True
            except OSError:
                checks[f"port_{port}_free"] = False
    checks["docker_daemon"] = bool(checks["docker"] and subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=15, check=False, env=child_environment()).returncode == 0)
    checks["element_commit"] = subprocess.run(
        ["git", "-C", "/home/sinkerine/dev/element-web", "cat-file", "-e", ELEMENT_SHA + "^{commit}"],
        capture_output=True, timeout=15, check=False, env=child_environment()).returncode == 0
    checks["element_built"] = (SB / "element-src/apps/web/webapp/index.html").is_file()
    write_json(SB / "evidence/preflight.json", checks)
    for key, value in checks.items():
        print(f"{key}: {'available' if value else 'unavailable'}")
    required = [key for key in checks if key not in ("pnpm", "element_built")]
    required.append("pnpm" if build else "element_built")
    return all(checks[key] for key in required)


def start(name, args):
    receipt = SB / "run" / (name + ".json")
    assert not receipt.exists(), "Stop the previous sandbox run first"
    if name == "provider":
        write_json(SB / "run/provider-mode.json", {"mode": "live" if "--live" in args else "deterministic"})
    with (SB / "evidence" / (name + ".log")).open("ab") as output:
        launch(args, receipt, OWNER, stdout=output, stderr=output)


def stop(name):
    stop_receipt(SB / "run" / (name + ".json"), OWNER)


def owned_container():
    result = subprocess.run(["docker", "inspect", "--format", '{{index .Config.Labels "' + LABEL + '"}}', CONTAINER],
                            capture_output=True, text=True, encoding="utf-8", timeout=15, check=False, env=child_environment())
    if result.returncode:
        return False
    assert result.stdout.strip() == OWNER, "Container name belongs to somebody else"
    return True


def synapse():
    assert not owned_container(), "Stop existing sandbox first"
    data = SB / "synapse"
    assert not data.is_symlink() and data.resolve().is_relative_to(SB)
    data.mkdir(exist_ok=True)
    marker = data / ".sandbox-owner"
    if marker.exists():
        assert marker.read_text(encoding="utf-8-sig") == OWNER
    else:
        assert not any(data.iterdir()), "Refuse to adopt pre-existing Synapse data"
        private_write(marker, OWNER)
    common = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",  # windows-footgun: ok — Linux-only harness
              "--label", f"{LABEL}={OWNER}", "-v", f"{data}:/data"]
    if not (data / "homeserver.yaml").exists():
        with (SB / "evidence/synapse-generate.log").open("ab") as output:
            run([*common, "-e", "SYNAPSE_SERVER_NAME=localhost", "-e", "SYNAPSE_REPORT_STATS=no",
                 SYNAPSE, "generate"], stdout=output, stderr=output)
        config_path = data / "homeserver.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))
        config.update(enable_registration=False, registration_shared_secret=secrets.token_hex(32),
                      report_stats=False, public_baseurl=HS + "/", signing_key_path="/data/localhost.signing.key")
        # Bind Docker's published endpoint only to loopback; the listener is inside the container.
        config["listeners"] = [{"port": 8008, "tls": False, "type": "http", "x_forwarded": True,
                                "resources": [{"names": ["client"], "compress": False}]}]
        private_write(config_path, yaml.safe_dump(config))
    run([*common, "-d", "--name", CONTAINER, "-p", "127.0.0.1:18008:8008", SYNAPSE], capture_output=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            request(HS + "/_matrix/client/versions")
            return
        except OSError:
            time.sleep(0.5)
    raise ValueError("Synapse readiness timeout")


def element(build):
    source = SB / "element-src"
    if build:
        assert not source.exists(), "Keep existing build or explicitly purge before rebuilding"
        source.mkdir()
        archive = SB / "tmp/element.tar"
        with archive.open("wb") as output:
            run(["git", "-C", "/home/sinkerine/dev/element-web", "archive", ELEMENT_SHA], stdout=output)
        run(["tar", "-xf", str(archive), "-C", str(source)])
        archive.unlink()
        with (SB / "evidence/element-build.log").open("ab") as output:
            # Root postinstall builds Desktop native modules, which Web does not use.
            run(["pnpm", "install", "--frozen-lockfile", "--ignore-scripts", "--store-dir", str(SB / "cache/pnpm-store")],
                cwd=source, stdout=output, stderr=output, timeout=1800)
            # Git dependencies need their prepared output even when workspace hooks are skipped.
            run(["pnpm", "--filter", "element-web", "rebuild", "@element-hq/element-call-component", "matrix-js-sdk"],
                cwd=source, stdout=output, stderr=output, timeout=1800)
            # Avoid pnpm's automatic reinstall re-enabling that root postinstall.
            run(["pnpm", "--config.verifyDepsBeforeRun=false", "--filter", "element-web", "run", "build"],
                cwd=source, stdout=output, stderr=output, timeout=1800)
        private_write(source / ".sandbox-commit", ELEMENT_SHA)
    web = configure_element()
    start("element", [sys.executable, "-m", "http.server", "18081", "--bind", "127.0.0.1", "--directory", str(web)])


def configure_element():
    source = SB / "element-src"
    assert (source / ".sandbox-commit").read_text(encoding="utf-8-sig") == ELEMENT_SHA
    web = source / "apps/web/webapp"
    assert (web / "index.html").is_file()
    config = {"default_server_config": {"m.homeserver": {"base_url": HS, "server_name": "localhost"}},
              "disable_custom_urls": True, "show_labs_settings": True,
              # Pinned Element maps 'labs' to no config override, allowing device-level toggles.
              "features": {"feature_interactive_ui": "labs"}, "brand": "Hermes disposable sandbox"}
    write_json(web / "config.json", config)
    return web


def down(purge):
    if purge:
        assert (SB / ".harness-owner").read_text(encoding="utf-8-sig") == OWNER
    for receipt in sorted((SB / "run").glob("command-*.json")):
        stop_receipt(receipt, OWNER)
    for name in ("gateway", "element", "provider"):
        stop(name)
    if owned_container():
        with (SB / "evidence/synapse.log").open("ab") as output:
            run(["docker", "logs", CONTAINER], stdout=output, stderr=output)
        run(["docker", "stop", "--time", "15", CONTAINER], capture_output=True)
    if purge:
        # PM's supplied interpreter and earlier task evidence share SB. Purge only harness-owned paths.
        for name in ("synapse", "element-src", "accounts.json", "bot-recovery-key.txt", "run", "assets", "replay"):
            path = SB / name
            assert not path.is_symlink() and path.resolve().is_relative_to(SB)
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        for name in (".env", "config.yaml", "platforms/matrix", "sessions", "state.db", "state.db-wal", "state.db-shm"):
            path = HOME / name
            assert not path.is_symlink() and path.resolve().is_relative_to(HOME)
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
    print("Owned services stopped; evidence retained.")


def provider_ready():
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            request("http://127.0.0.1:18082/v1/models")
            return
        except OSError:
            time.sleep(0.2)
    raise ValueError("Provider readiness timeout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "down", "restart-gateway", "preflight", "configure-element"))
    parser.add_argument("--build-element", action="store_true")
    parser.add_argument("--live", action="store_true", help="Actually access Magpie; default total cap 30")
    parser.add_argument("--live-request-limit", type=live_request_limit,
                        help="Explicitly authorized total cap (1..42); requires up --live")
    parser.add_argument("--purge", action="store_true")
    args = parser.parse_args()
    if args.live_request_limit is not None and not (args.command == "up" and args.live):
        parser.error("--live-request-limit requires up --live")
    if args.command == "configure-element":
        configure_element()
        return
    if args.command == "down":
        down(args.purge)
        return
    if args.command == "restart-gateway":
        try:
            stop("gateway")
            start("gateway", [sys.executable, "-m", "gateway.run"])
        except BaseException:
            down(False)
            raise
        return
    ready = preflight(args.build_element)
    if args.command == "preflight":
        return
    assert ready, "Resolve preflight blockers before starting services"
    assert not any((SB / f"run/{name}.json").exists() for name in ("gateway", "element", "provider")), "Stop previous run first"
    marker = SB / ".harness-owner"
    if marker.exists():
        assert marker.read_text(encoding="utf-8-sig") == OWNER
    else:
        assert not (HOME / ".env").exists() and not (HOME / "config.yaml").exists(), "Refuse to overwrite existing config"
        private_write(marker, OWNER)
    try:
        synapse()
        run([sys.executable, str(HERE / "bootstrap.py")])
        element(args.build_element)
        provider = [sys.executable, str(HERE / "fake_openai.py")]
        if args.live:
            provider.append("--live")
            if args.live_request_limit is not None:
                provider.extend(["--live-request-limit", str(args.live_request_limit)])
        start("provider", provider)
        provider_ready()
        start("gateway", [sys.executable, "-m", "gateway.run"])
    except BaseException:
        down(False)
        raise
    print("Sandbox starting: Element http://127.0.0.1:18081 (inspect private evidence for readiness).")


def interrupted(signum, _frame):
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    entrypoint(main)

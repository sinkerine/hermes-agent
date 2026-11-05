"""Room project management through the native command and real profile database."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig, load_gateway_config
from gateway.platforms.event import MessageEvent
from gateway.project_routing import ProjectPin
from gateway.session import SessionSource
from gateway.slash_access import policy_for_runner_source
from gateway.slash_commands_project import GatewayProjectCommandsMixin
from hermes_cli import projects_db as db
from tools.terminal_scope import reset_terminal_scope, set_terminal_scope


class Runner(GatewayProjectCommandsMixin):
    def __init__(self, home, root):
        self.home = home
        self.config = GatewayConfig(project_workspace_root=str(root))
        self.pin = None
        self.session_store = SimpleNamespace(get_session_metadata=lambda *args: self.pin)
        self.scoped = False
        self.backend = "local"

    def _resolve_profile_home_for_source(self, source):
        assert self.scoped
        return self.home

    def _session_key_for_source(self, source):
        return "room-conversation"

    def _check_slash_access(self, source, command):
        policy = policy_for_runner_source(self, source)
        return None if policy.is_admin(source.user_id) or command in policy.user_allowed_commands else "denied"

    @contextmanager
    def _profile_scope_for_source(self, source):
        token = set_terminal_scope({"TERMINAL_ENV": self.backend})
        self.scoped = True
        try:
            yield
        finally:
            self.scoped = False
            reset_terminal_scope(token)

    @asynccontextmanager
    async def _async_profile_scope_for_source(self, source):
        yield


@pytest.fixture
def runner(tmp_path):
    home = tmp_path / "profile"; home.mkdir()
    root = tmp_path / "workspaces"; root.mkdir()
    return Runner(home, root)


async def call(runner, args="", user="admin"):
    source = SessionSource(platform=Platform.MATRIX, chat_id="room", user_id=user, chat_type="group")
    return await runner._handle_project_command(MessageEvent(text="/project " + args, source=source))


def project(runner, slug):
    path = runner.home / slug; path.mkdir()
    with db.connect_closing(db_path=runner.home / "projects.db") as conn:
        return db.create_project(conn, name=slug.title(), slug=slug, primary_path=str(path))


@pytest.mark.asyncio
async def test_create_retry_bind_force_pin_and_unbind(runner):
    assert "Created project alpha" in await call(runner, 'create alpha --name "Alpha Team"')
    agents = Path(runner.config.project_workspace_root) / "alpha" / "AGENTS.md"
    agents.write_text("user instructions")
    assert "Reused project alpha" in await call(runner, "create alpha")
    assert agents.read_text() == "user instructions"
    assert "already bound" in await call(runner, "bind alpha")
    beta = project(runner, "beta")
    assert "--force" in await call(runner, "bind beta")
    with db.connect_closing(db_path=runner.home / "projects.db") as conn:
        alpha = db.get_project(conn, "alpha")
    runner.pin = ProjectPin(alpha.id, slug="alpha", cwd=alpha.primary_path).to_dict()
    assert "Bound this room to beta" in await call(runner, "bind " + beta + " --force")
    info = await call(runner)
    assert "Room project: Beta" in info and "This conversation: alpha" in info and "/new" in info
    assert runner.pin["project_id"] == alpha.id
    assert "* Beta" in await call(runner, "list")
    assert "No files or transcripts" in await call(runner, "unbind")
    assert agents.exists()
    assert "no project binding" in await call(runner, "unbind")


@pytest.mark.asyncio
@pytest.mark.parametrize("same_project", [False, True], ids=["different-projects", "same-project"])
async def test_concurrent_thread_binds_are_atomic_in_runtime_profile(tmp_path, monkeypatch, same_project):
    from gateway import slash_commands_project as commands
    from gateway.project_routing import binding_key
    from gateway.run import GatewayRunner
    from gateway.session_identity import RoutingIdentity
    from hermes_cli.config import atomic_config_write
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import terminal_env

    homes = {name: tmp_path / name for name in ("launch", "receiving", "runtime")}
    for name, home in homes.items():
        home.mkdir()
        atomic_config_write(home / "config.yaml", {"terminal": {"env": "local" if name == "runtime" else "docker"}})
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(homes["launch"]))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._primary_profile_name = "default"
    runner._profile_configs = {"receiving": GatewayConfig(platforms={
        Platform.MATRIX: PlatformConfig(extra={"group_allow_admin_from": ["admin"]})})}
    runner.session_store = SimpleNamespace(get_session_metadata=lambda *args: None)
    sources = [SessionSource(platform=Platform.MATRIX, chat_id="!room:test", thread_id=thread,
                             user_id="admin", chat_type="group", profile="runtime")
               for thread in ("$first", "$second")]
    for source in sources:
        source._identity = RoutingIdentity(transport_profile="receiving", runtime_profile="runtime",
                                           authorization_home=homes["receiving"], runtime_home=homes["runtime"])
    assert runner._session_key_for_source(sources[0]) != runner._session_key_for_source(sources[1])
    assert binding_key(runner, sources[0]) == binding_key(runner, sources[1])
    ids = {}
    with db.connect_closing(db_path=homes["runtime"] / "projects.db") as conn:
        for slug in ("alpha", "beta"):
            folder = homes["runtime"] / slug
            folder.mkdir()
            ids[slug] = db.create_project(conn, name=slug.title(), slug=slug, primary_path=str(folder))

    requests_ready, reads_ready = Barrier(2), Barrier(2)
    validate, read_binding = commands.validate_project, commands._binding

    def coordinated_validate(project, ref):
        assert get_hermes_home() == homes["runtime"]
        assert terminal_env("TERMINAL_ENV") == "local"
        cwd = validate(project, ref)
        requests_ready.wait(timeout=10)
        return cwd

    def coordinated_read(conn, key):
        binding = read_binding(conn, key)
        # A transaction already serializes reads; waiting there would deadlock its writer.
        if not conn.in_transaction:
            reads_ready.wait(timeout=10)
        return binding

    async def bind(source, slug, force=False):
        event = MessageEvent(text=f"/project bind {slug}" + (" --force" if force else ""), source=source)
        return await runner._handle_project_command(event)

    slugs = ["alpha", "alpha" if same_project else "beta"]
    with monkeypatch.context() as patch:
        patch.setattr(commands, "validate_project", coordinated_validate)
        patch.setattr(commands, "_binding", coordinated_read)
        replies = await asyncio.gather(*(bind(source, slug) for source, slug in zip(sources, slugs)))
    successes = [i for i, reply in enumerate(replies) if reply.startswith("Bound this room to ")]
    assert len(successes) == 1, replies
    winner = successes[0]
    loser = 1 - winner
    assert ("already bound" if same_project else "--force") in replies[loser]
    key = {"platform": "matrix", "transport_profile": "receiving", "chat_id": "!room:test"}
    with db.connect_closing(db_path=homes["runtime"] / "projects.db") as conn:
        binding = db.get_chat_binding(conn, **key)
        assert binding.project_id == ids[slugs[winner]]
        assert binding.bound_by == "admin"
    assert "already bound" in await bind(sources[loser], slugs[winner])
    with db.connect_closing(db_path=homes["runtime"] / "projects.db") as conn:
        assert db.get_chat_binding(conn, **key) == binding
    replacement = "beta" if slugs[winner] == "alpha" else "alpha"
    assert "--force" in await bind(sources[loser], replacement)
    assert "Bound this room to " + replacement in await bind(sources[loser], replacement, force=True)
    with db.connect_closing(db_path=homes["runtime"] / "projects.db") as conn:
        assert db.get_chat_binding(conn, **key).project_id == ids[replacement]
    assert get_hermes_home() == homes["launch"]
    assert not (homes["launch"] / "projects.db").exists()
    assert not (homes["receiving"] / "projects.db").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_agents", [True, False], ids=["complete-scaffold", "partial-scaffold"])
async def test_concurrent_thread_creates_reuse_one_project(tmp_path, monkeypatch, existing_agents):
    from gateway.run import GatewayRunner
    from gateway.session_identity import RoutingIdentity
    from hermes_cli.config import atomic_config_write

    home = tmp_path / "runtime"
    home.mkdir()
    root = tmp_path / "workspaces"
    target = root / "alpha"
    target.mkdir(parents=True)
    (target / "sources").mkdir()
    agents = target / "AGENTS.md"
    if existing_agents:
        agents.write_text("user instructions")
    atomic_config_write(home / "config.yaml", {"terminal": {"env": "local"}})
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True, project_workspace_root=str(root))
    runner._primary_profile_name = "default"
    runner._profile_configs = {"receiving": GatewayConfig(platforms={
        Platform.MATRIX: PlatformConfig(extra={"group_allow_admin_from": ["admin"]})})}
    runner.session_store = SimpleNamespace(get_session_metadata=lambda *args: None)
    sources = [SessionSource(platform=Platform.MATRIX, chat_id=room, thread_id=thread,
                             user_id="admin", chat_type="group", profile="runtime")
               for room, thread in (("!first:test", "$first"), ("!second:test", "$second"))]
    for source in sources:
        source._identity = RoutingIdentity(transport_profile="receiving", runtime_profile="runtime",
                                           authorization_home=home, runtime_home=home)
    assert runner._session_key_for_source(sources[0]) != runner._session_key_for_source(sources[1])
    # Initialize before the workers so the coordinated race is project creation, not schema setup.
    with db.connect_closing(db_path=home / "projects.db") as conn:
        assert db.list_projects(conn) == []
    reads_ready, agents_ready = Barrier(2), Barrier(2)
    find, exists = db.find_by_primary_path, Path.exists

    def coordinated_find(conn, path, **kwargs):
        result = find(conn, path, **kwargs)
        # The final lookup must serialize; a barrier under its write lock would deadlock.
        if not conn.in_transaction and (kwargs.get("include_archived") or existing_agents):
            reads_ready.wait(timeout=10)
        return result

    def coordinated_exists(path):
        result = exists(path)
        if path == agents and not existing_agents:
            agents_ready.wait(timeout=10)
        return result

    async def create(source):
        return await runner._handle_project_command(MessageEvent(text="/project create alpha", source=source))

    with monkeypatch.context() as patch:
        patch.setattr(db, "find_by_primary_path", coordinated_find)
        patch.setattr(Path, "exists", coordinated_exists)
        replies = await asyncio.gather(*(create(source) for source in sources))
    with db.connect_closing(db_path=home / "projects.db") as conn:
        projects = db.list_projects(conn, include_archived=True)
        assert len(projects) == 1, [(p.id, p.slug, p.primary_path) for p in projects]
        assert sum(reply.startswith("Created project alpha.\n") for reply in replies) == 1, replies
        assert sum(reply.startswith("Reused project alpha.\n") for reply in replies) == 1, replies
        project = projects[0]
        assert project.slug == "alpha" and project.primary_path == str(target)
        for source in sources:
            binding = db.get_chat_binding(conn, platform="matrix", transport_profile="receiving",
                                          chat_id=source.chat_id)
            assert binding.project_id == project.id and binding.bound_by == "admin"
    assert agents.read_text() == ("user instructions" if existing_agents else
                                  "# alpha\n\nKeep project source material in sources/.\n")


@pytest.mark.asyncio
async def test_create_force_and_retry_after_partial_scaffold(runner):
    project(runner, "existing")
    await call(runner, "bind existing")
    assert "--force" in await call(runner, "create alpha")
    root = Path(runner.config.project_workspace_root)
    agents = root / "alpha" / "AGENTS.md"
    agents.unlink()
    assert "Reused project alpha" in await call(runner, "create alpha --force")
    assert agents.is_file() and (root / "alpha" / "sources").is_dir()
    assert "Room project: alpha" in await call(runner, "info")
    with db.connect_closing(db_path=runner.home / "projects.db") as conn:
        assert [p.slug for p in db.list_projects(conn)].count("alpha") == 1


@pytest.mark.asyncio
async def test_admin_policy_and_slash_access(runner):
    runner.config.platforms[Platform.MATRIX] = PlatformConfig(extra={
        "group_allow_admin_from": ["admin"], "group_user_allowed_commands": ["project"]})
    for args in ("create alpha", "bind alpha", "unbind"):
        assert "Only an admin" in await call(runner, args, "guest")
    assert "unbound" in await call(runner, "info", "guest")
    assert "No non-archived" in await call(runner, "list", "guest")
    runner.config.platforms[Platform.MATRIX].extra = {"group_allow_admin_from": ["admin"]}
    assert await call(runner, "info", "guest") == "denied"
    runner.config.platforms.clear()
    assert "Created project alpha" in await call(runner, "create alpha", "guest")


@pytest.mark.asyncio
async def test_path_safety_conflict_and_backend(runner, tmp_path):
    root = runner.config.project_workspace_root
    runner.config.project_workspace_root = None
    assert "creation is disabled" in await call(runner, "create alpha")
    runner.config.project_workspace_root = root
    for slug in ("../escape", "a/b", "..", "a\\b"):
        assert "Invalid slug" in await call(runner, "create " + repr(slug))
    (Path(root) / "escape").symlink_to(tmp_path, target_is_directory=True)
    assert "Unsafe workspace" in await call(runner, "create escape")
    project(runner, "alpha")
    assert "already uses another folder" in await call(runner, "create alpha")
    (Path(root) / "child").mkdir()
    (Path(root) / "child" / "AGENTS.md").symlink_to(tmp_path / "outside")
    assert "Unsafe workspace" in await call(runner, "create child")
    assert not (tmp_path / "outside").exists()
    runner.backend = "docker"
    assert "local terminal backend" in await call(runner, "bind alpha")
    assert "local terminal backend" in await call(runner, "create fresh")
    assert not (Path(root) / "fresh").exists()


@pytest.mark.asyncio
async def test_dangling_id_never_resolves_slug_and_archived_hidden(runner):
    pid = project(runner, "alpha")
    assert "Bound" in await call(runner, "bind alpha")
    with db.connect_closing(db_path=runner.home / "projects.db") as conn:
        db.delete_project(conn, pid)
        shadow = db.create_project(conn, name="Shadow", slug=pid, primary_path=str(runner.home))
        db.archive_project(conn, shadow)
    assert "missing project " + pid in await call(runner, "info")
    assert "No non-archived" in await call(runner, "list")
    assert "archived" in await call(runner, "bind " + shadow)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["help", "wat", "bind", "create x --profile other", "info extra", 'create "'])
async def test_usage(runner, args):
    assert (await call(runner, args)).startswith("/project [")


def test_registry_idle_handler_and_matrix_bang():
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.slash_commands import GatewaySlashCommandsMixin
    from hermes_cli.commands import resolve_command, is_gateway_known_command
    from plugins.platforms.matrix.adapter import _normalize_matrix_bang_command
    assert resolve_command("project").gateway_only
    assert is_gateway_known_command("project")
    assert "project" in GatewayBusySessionMixin._IDLE_COMMANDS
    assert GatewaySlashCommandsMixin._handle_project_command is GatewayProjectCommandsMixin._handle_project_command
    assert _normalize_matrix_bang_command("!project bind x") == "/project bind x"


def test_workspace_root_through_real_gateway_loader(tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    atomic_config_write(tmp_path / "config.yaml", {"gateway": {"project_routing": {"workspace_root": "  /work/projects  "}}})
    config = load_gateway_config()
    assert config.project_workspace_root == "/work/projects"
    assert GatewayConfig.from_dict(config.to_dict()).project_workspace_root == "/work/projects"
    assert GatewayConfig.from_dict({"project_routing": {"workspace_root": " "}}).project_workspace_root is None

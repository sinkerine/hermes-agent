"""Frozen plugins respect generic platform restrictions through real discovery."""

import pytest

from hermes_cli import plugins
from hermes_cli import tools_config


FROZEN_PLUGIN = '''
def register(ctx):
    for name in ("interactive_ui", "restricted_probe"):
        ctx.register_tool(name=name + "_probe", toolset=name,
            schema={"name": name + "_probe", "description": "probe",
                    "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: "ok")
'''


@pytest.fixture
def discovered(tmp_path, monkeypatch):
    home = tmp_path / "home"
    plugin = home / "plugins" / "frozen_surface"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text('name: frozen_surface\nversion: "1.0"\n')
    (plugin / "__init__.py").write_text(FROZEN_PLUGIN)
    (home / "config.yaml").write_text('plugins:\n  enabled: [frozen_surface]\n  isolation: in_process\n')
    monkeypatch.setenv("HERMES_HOME", str(home))
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: bundled)
    monkeypatch.setitem(tools_config._TOOLSET_PLATFORM_RESTRICTIONS, "restricted_probe", {"matrix"})
    plugins.discover_plugins(force=True)
    assert {"interactive_ui", "restricted_probe"} <= tools_config._get_plugin_toolset_keys()
    yield
    plugins.get_plugin_manager().unload()


@pytest.mark.parametrize("platform", ["matrix", "telegram", "cli", "discord", "local"])
@pytest.mark.parametrize("selection", [None, [], ["all"], ["hermes-cli"], ["interactive_ui", "restricted_probe"]])
def test_frozen_plugin_surface(discovered, platform, selection):
    config = {} if selection is None else {"platform_toolsets": {platform: selection}}
    enabled = tools_config._get_platform_tools(config, platform)
    assert ("interactive_ui" in enabled) == (platform == "matrix" and selection == ["interactive_ui", "restricted_probe"])
    assert ("restricted_probe" in enabled) == (platform == "matrix")


def test_platform_restriction_covers_explicit_and_implicit_plugins(discovered):
    for selection in ([], ["restricted_probe"], ["all"], ["hermes-cli"]):
        enabled = tools_config._get_platform_tools({"platform_toolsets": {"telegram": selection}}, "telegram")
        assert "restricted_probe" not in enabled


def test_saved_selection_stays_fixed_until_resolved_again(discovered):
    config = {"platform_toolsets": {"matrix": []}}
    selected = tools_config._get_platform_tools(config, "matrix")
    config["platform_toolsets"]["matrix"].append("interactive_ui")
    assert "interactive_ui" not in selected
    assert "interactive_ui" in tools_config._get_platform_tools(config, "matrix")

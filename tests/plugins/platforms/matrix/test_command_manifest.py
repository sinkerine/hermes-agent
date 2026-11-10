"""MSC4391 manifests preserve dispatch spelling and Element's wire constraints."""

import base64
import hashlib
import re
from types import SimpleNamespace

import pytest

from plugins.platforms.matrix import adapter_commands as commands

BOT = "@hermes:localhost"


def _element_accepts(content):
    """Independent mirror of the consumer checks for our primitive-string subset."""
    token = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    tokens = content["command"].split(" ")
    assert len(tokens) <= 3 and all(token.fullmatch(value) for value in tokens)
    aliases = content.get("aliases", [])
    assert isinstance(aliases, list) and len(aliases) <= 10
    assert all(isinstance(alias, str) and token.fullmatch(alias) for alias in aliases)
    parameters = content["parameters"]
    assert isinstance(parameters, list) and len(parameters) <= 32
    keys = set()
    for parameter in parameters:
        key = parameter["key"]
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key) and key not in keys
        keys.add(key)
        assert parameter["schema"] == {"schema_type": "primitive", "type": "string"}
        assert isinstance(parameter["optional"], bool)


def _install_skill(home, name):
    directory = home / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Skill {name}\n---\nDo the task.\n")


def test_real_discovery_includes_gateway_plugin_and_matrix_skills(tmp_path, monkeypatch):
    from hermes_cli.plugins import get_plugin_manager
    from agent.skill_commands import reload_skills
    from tools import skills_tool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path / "skills")
    (tmp_path / "config.yaml").write_text("plugins:\n  enabled: [command-test]\nskills:\n  platform_disabled:\n    matrix: [hidden]\n")
    for name in ("research", "status", "hidden"):
        _install_skill(tmp_path, name)
    plugin = tmp_path / "plugins" / "command-test"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: command-test\nversion: 1.0.0\n")
    (plugin / "__init__.py").write_text(
        "def register(ctx):\n"
        "    ctx.register_command('plugin-probe', lambda args: 'ok', description='Plugin probe')\n"
        "    ctx.register_command('bad.name', lambda args: 'ok')\n"
        f"    ctx.register_command({'x' * 65!r}, lambda args: 'ok')\n"
    )
    manager = get_plugin_manager()
    try:
        manager.discover_and_load()
        reload_skills()
        manifest = commands.build_command_manifest(BOT)
        by_name = {content["command"]: content for content in manifest.values()}
        assert {"status", "research", "plugin-probe"} <= by_name.keys()
        assert "hidden" not in by_name
        assert "bad.name" not in by_name and "x" * 65 not in by_name
        assert by_name["status"]["description"] != {"m.text": [{"body": "Skill status"}]}
        assert by_name["status"]["parameters"] == []
        parameter = by_name["egress"]["parameters"][0]
        assert parameter["key"] == "args" and parameter["optional"] is True
        assert parameter["description"] == {"m.text": [{"body": "[status]"}]}
        assert by_name["research"]["parameters"][0]["key"] == "args"
        assert by_name["plugin-probe"]["parameters"][0]["optional"] is True
        names = []
        for key, content in manifest.items():
            _element_accepts(content)
            assert commands.is_valid_description(content)
            expected = base64.b64encode(hashlib.sha256((content["command"] + BOT).encode()).digest()).decode()
            assert key == expected and len(key) == 44 and key.endswith("=")
            names.extend([content["command"], *content.get("aliases", [])])
        assert len(names) == len(set(names)) and len(manifest) <= 500
    finally:
        manager.unload()


@pytest.mark.parametrize("hint,subcommands,default_args,key,optional,description", [
    ("", (), False, None, None, None),
    ("", (), True, "args", True, None),
    ("<model-name>", (), False, "model_name", False, None),
    ("[focus instructions]", (), False, "focus_instructions", True, None),
    ("[status]", ("status",), False, "args", True, "[status]"),
    ("<a|b>", (), False, "args", False, "<a|b>"),
    ("[on|off] [name]", (), False, "args", True, "[on|off] [name]"),
    ("<model> [provider]", (), False, "args", False, "<model> [provider]"),
    ("[" + "a" * 64 + "]", (), False, "args", True, "[" + "a" * 64 + "]"),
])
def test_parameter_rules(hint, subcommands, default_args, key, optional, description):
    parameters = commands.command_parameters(hint, subcommands=subcommands, default_args=default_args)
    if key is None:
        assert parameters == []
        return
    assert parameters[0]["key"] == key
    assert parameters[0]["optional"] is optional
    assert parameters[0].get("description") == ({"m.text": [{"body": description}]} if description is not None else None)
    _element_accepts(commands.description_content("test", "Test", parameters))


def test_descriptions_collapse_whitespace_and_truncate():
    content = commands.description_content("test", "  one\n  two  " + "x" * 250, [])
    text = content["description"]["m.text"][0]["body"]
    assert text.startswith("one two ") and len(text) == 200 and text.endswith("...")
    assert "aliases" not in content


def test_precedence_alias_reservations_and_cap(monkeypatch):
    from hermes_cli import commands_platforms, plugins

    core = [SimpleNamespace(name="first", describe=lambda: "First", args_hint="", subcommands=(),
                            aliases=("later", "shared", *(f"alias{i}" for i in range(15)))),
            SimpleNamespace(name="later", describe=lambda: "Later", args_hint="", subcommands=(), aliases=("shared",))]
    monkeypatch.setattr(commands_platforms, "_gateway_available_commands", lambda: core)
    monkeypatch.setattr(plugins, "get_plugin_commands", lambda: {"shared": {}, "first": {}, "plugin": {}})
    skills = [(f"/skill{i:03}", {"description": "Skill"}, ()) for i in range(510)]
    skills.extend([(f"/{'z' * 65}", {}, ()), ("/bad.name", {}, ()), ("/plugin", {}, ())])
    monkeypatch.setattr(commands_platforms, "_iter_gateway_skills", lambda platform: iter(skills))
    contents = list(commands.build_command_manifest(BOT).values())
    assert len(contents) == 500
    assert [content["command"] for content in contents[:3]] == ["first", "later", "plugin"]
    assert "later" not in contents[0]["aliases"] and "shared" in contents[0]["aliases"]
    assert len(contents[0]["aliases"]) == 10 and "aliases" not in contents[1]
    names = [name for content in contents for name in [content["command"], *content.get("aliases", [])]]
    assert len(names) == len(set(names))
    assert all(commands.is_valid_description(content) for content in contents)


def test_one_failed_source_keeps_other_sources(monkeypatch, caplog):
    from hermes_cli import commands_platforms, plugins

    def broken():
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(plugins, "get_plugin_commands", broken)
    monkeypatch.setattr(commands_platforms, "_iter_gateway_skills", lambda platform: iter([]))
    assert any(content["command"] == "status" for content in commands.build_command_manifest(BOT).values())
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("change", [
    {"command": "bad.name"}, {"command": "x" * 65}, {"aliases": ["a"] * 11},
    {"aliases": [None]}, {"parameters": None},
    {"parameters": [{"key": "bad-key", "schema": {}, "optional": True}]},
    {"parameters": [{"key": "args", "schema": {"schema_type": "primitive", "type": "string"}, "optional": "yes"}]},
])
def test_validator_rejects_unusable_content(change):
    content = commands.description_content("test", "Test", [])
    content.update(change)
    assert not commands.is_valid_description(content)

"""The worker tells the frontend what each command does, and in what order to list them."""
from types import SimpleNamespace

from aria_code.apps.cli.app_server import Server
from aria_code.apps.cli.commands.catalog import CORE_SLASH_COMMANDS, ENTRY_SLASH_COMMANDS


def _server(registry, lang="en"):
    terminal = SimpleNamespace(commands=SimpleNamespace(commands=registry), config={"ui_lang": lang})
    return SimpleNamespace(terminal=terminal)


def _registry(*names):
    return {name: (None, f"Does {name[1:]}.") for name in names}


def test_core_and_entry_commands_come_first_then_the_rest_alphabetically():
    names = [*CORE_SLASH_COMMANDS[:3], *ENTRY_SLASH_COMMANDS[:2], "/zebra", "/alpha", "/middle"]
    info = Server.commands_info(_server(_registry(*names)))
    listed = [item["name"] for item in info]
    assert listed[:5] == [*CORE_SLASH_COMMANDS[:3], *ENTRY_SLASH_COMMANDS[:2]]
    assert listed[5:] == ["/alpha", "/middle", "/zebra"]


def test_every_command_appears_once_and_has_a_bounded_description():
    registry = _registry("/model", "/alpha")
    registry["/long"] = (None, "x" * 500)
    info = Server.commands_info(_server(registry))
    assert sorted(i["name"] for i in info) == ["/alpha", "/long", "/model"]
    assert all(len(i["description"]) <= 160 for i in info)
    assert all(isinstance(i["description"], str) for i in info)


def test_a_command_the_catalog_does_not_know_still_gets_its_own_description():
    info = Server.commands_info(_server(_registry("/alpha")))
    assert info == [{"name": "/alpha", "description": "Does alpha."}]


def test_descriptions_follow_the_ui_language_when_a_translation_exists():
    english = {i["name"]: i["description"] for i in Server.commands_info(_server(_registry("/model", "/help"), "en"))}
    chinese = {i["name"]: i["description"] for i in Server.commands_info(_server(_registry("/model", "/help"), "zh"))}
    assert english.keys() == chinese.keys()
    assert chinese["/help"] != english["/help"], "the Chinese list should not show English descriptions"

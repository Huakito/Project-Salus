import json

import pytest

from salus.contract import Command, ContractError, load_contract

MINIMAL = {
    "screen": {"width": 320, "height": 240},
    "commands": [{"key": "beep", "label": "Beep"}],
}


def write(tmp_path, content):
    path = tmp_path / "commands.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def test_the_real_commands_json_is_valid():
    contract = load_contract()
    assert (contract.screen.width, contract.screen.height) == (160, 80)  # the robot's st7735 screen
    assert contract.command("face").editor == "face"
    assert contract.command("no_such_command") is None
    # Every example in the file is a value its own command accepts.
    for command in contract.commands:
        command.check(command.example)


def test_a_minimal_file_loads_with_defaults(tmp_path):
    contract = load_contract(write(tmp_path, MINIMAL))
    assert contract.command("beep").value == "none"
    assert contract.events == []


def test_a_missing_file_is_explained(tmp_path):
    with pytest.raises(ContractError, match="commands.json is missing"):
        load_contract(tmp_path / "commands.json")


def test_broken_json_says_where(tmp_path):
    with pytest.raises(ContractError, match=r"not valid JSON: .* \(line 2, column 3\)"):
        load_contract(write(tmp_path, "{\n  oops\n}"))


def test_a_misspelt_field_is_reported_not_ignored(tmp_path):
    bad = {**MINIMAL, "commands": [{"key": "beep", "label": "Beep", "desciption": "oops"}]}
    with pytest.raises(ContractError, match=r"commands\[0\]\.desciption"):
        load_contract(write(tmp_path, bad))


def test_an_unknown_kind_of_value_is_reported(tmp_path):
    bad = {**MINIMAL, "commands": [{"key": "beep", "label": "Beep", "value": "string"}]}
    with pytest.raises(ContractError, match=r"commands\[0\]\.value"):
        load_contract(write(tmp_path, bad))


def test_a_command_listed_twice_is_reported(tmp_path):
    bad = {**MINIMAL, "commands": MINIMAL["commands"] * 2}
    with pytest.raises(ContractError, match='command "beep" is listed twice'):
        load_contract(write(tmp_path, bad))


def test_a_key_the_firmware_could_not_have_is_reported(tmp_path):
    bad = {**MINIMAL, "commands": [{"key": "play melody", "label": "Play"}]}
    with pytest.raises(ContractError, match=r"commands\[0\]\.key"):
        load_contract(write(tmp_path, bad))


def command(value, **extra) -> Command:
    return Command(key="thing", label="Thing", value=value, **extra)


def test_a_command_without_a_value_sends_an_empty_one():
    assert command("none").check(None) == ""
    assert command("none").check("") == ""
    with pytest.raises(ValueError, match="takes no value"):
        command("none").check("x")


def test_a_text_command_needs_text():
    assert command("text").check("c,e,g") == "c,e,g"
    with pytest.raises(ValueError, match="needs text"):
        command("text").check(5)


def test_a_number_command_keeps_within_its_range():
    volume = command("number", min=0, max=1)
    assert volume.check(0) == 0
    assert volume.check(0.4) == 0.4
    assert volume.check(1) == 1
    with pytest.raises(ValueError, match="at least 0"):
        volume.check(-0.1)
    with pytest.raises(ValueError, match="at most 1"):
        volume.check(1.5)


@pytest.mark.parametrize("not_a_number", ["0.4", None, True, float("nan"), float("inf"), [1]])
def test_a_number_command_needs_a_real_number(not_a_number):
    with pytest.raises(ValueError, match="needs a number"):
        command("number").check(not_a_number)


def test_a_json_command_needs_an_object_or_a_list():
    assert command("json").check({"r": 255}) == {"r": 255}
    assert command("json").check([1, 2]) == [1, 2]
    with pytest.raises(ValueError, match="JSON object or list"):
        command("json").check("{}")


def test_a_command_can_be_kept_off_the_page(tmp_path):
    content = {**MINIMAL, "commands": [{"key": "beep", "label": "Beep"}, {"key": "hush", "label": "Hush", "show": False}]}
    contract = load_contract(write(tmp_path, content))
    assert [command.show for command in contract.commands] == [True, False]
    assert contract.command("hush") is not None  # still a command the app may send

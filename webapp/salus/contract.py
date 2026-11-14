"""commands.json: what the app and the robot's firmware have agreed on.

The file lists every command the firmware accepts and every event it sends. The
page builds its controls from it, the fake board answers according to it, and
the server refuses to send anything that is not in it. It is read again each
time it is needed, so an edit shows up on the next page refresh.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "commands.json"


class ContractError(Exception):
    """commands.json is missing, or does not make sense."""


class _Strict(BaseModel):
    # A misspelt field name is reported, not silently ignored.
    model_config = ConfigDict(extra="forbid")


class Screen(_Strict):
    """Size of the robot's screen in pixels, as the firmware has it rotated."""

    width: int = Field(gt=0)
    height: int = Field(gt=0)


class Command(_Strict):
    """One action the firmware accepts."""

    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    label: str = Field(min_length=1)
    description: str = ""
    value: Literal["none", "text", "number", "json"] = "none"
    min: float | None = None
    max: float | None = None
    step: float | None = Field(default=None, gt=0)
    example: Any = None
    # Set when the page has a purpose-built editor for this command ("face"),
    # so it is left out of the generic list of controls.
    editor: str | None = None

    def check(self, value: Any) -> Any:
        """Return ``value`` ready to send, or raise ValueError saying what is wrong with it."""
        if self.value == "none":
            if value not in (None, ""):
                raise ValueError(f"{self.key} takes no value.")
            return ""
        if self.value == "text":
            if not isinstance(value, str):
                raise ValueError(f"{self.key} needs text.")
            return value
        if self.value == "number":
            # bool is a kind of int in Python, and True is not a volume.
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{self.key} needs a number.")
            if self.min is not None and value < self.min:
                raise ValueError(f"{self.key} must be at least {self.min:g}.")
            if self.max is not None and value > self.max:
                raise ValueError(f"{self.key} must be at most {self.max:g}.")
            return value
        if not isinstance(value, (dict, list)):
            raise ValueError(f"{self.key} needs a JSON object or list.")
        return value


class EventSpec(_Strict):
    """One event the firmware sends."""

    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: Literal["sensor", "button", "state", "log"] = "sensor"
    description: str = ""


class Contract(_Strict):
    screen: Screen
    commands: list[Command]
    events: list[EventSpec] = []

    @model_validator(mode="after")
    def _keys_are_unique(self) -> Contract:
        for kind, items in (("command", self.commands), ("event", self.events)):
            seen: set[str] = set()
            for item in items:
                if item.key in seen:
                    raise ValueError(f'{kind} "{item.key}" is listed twice')
                seen.add(item.key)
        return self

    def command(self, key: str) -> Command | None:
        return next((command for command in self.commands if command.key == key), None)


def load_contract(path: Path = DEFAULT_PATH) -> Contract:
    """Read and check commands.json. Raises ContractError with a message fit to show."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ContractError(f"{path.name} is missing. It should be at {path}.") from None
    except json.JSONDecodeError as exc:
        raise ContractError(
            f"{path.name} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})."
        ) from None
    try:
        return Contract.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(_describe(error) for error in exc.errors())
        raise ContractError(f"{path.name} has a problem: {problems}.") from None


def _describe(error: dict) -> str:
    """Turn one pydantic error into e.g. ``commands[2].value: Input should be ...``."""
    where = ""
    for part in error["loc"]:
        where += f"[{part}]" if isinstance(part, int) else f".{part}"
    message = error["msg"].removeprefix("Value error, ")
    return f"{where.lstrip('.')}: {message}" if where else message

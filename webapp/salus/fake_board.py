"""A stand-in for the robot, for working on the app without the hardware.

FakeBoardTransport takes the place of the USB cable and of the firmware at the
far end of it. The real ``atech.Board`` runs on top of it, so ``board.call()``
and ``board.events()`` behave as they do with the robot plugged in: a command
listed in commands.json is acknowledged, and anything else is refused with
"unknown action", which is what the firmware answers.

It only pretends. A command passing here says nothing about the real firmware.
"""

from __future__ import annotations

import queue
import time
from typing import Callable

import serial
from atech.runtime import Action, Event

from .contract import Contract


class FakeBoardTransport:
    def __init__(self, load_contract: Callable[[], Contract]) -> None:
        self._load_contract = load_contract
        self._events: queue.Queue[Event] = queue.Queue()
        self._latest: dict[str, Event] = {}
        self._acks: dict[str, dict] = {}
        self.unplugged = False
        self.silent = False  # True: take commands without answering, like a board that is rebooting
        self.face: dict | None = None  # what the pretend screen is showing

    # ---- what atech.Board needs from a transport ----

    def send(self, action: Action) -> None:
        if self.unplugged:
            raise serial.SerialException("the fake board is unplugged")
        if self.silent:
            return
        refusal = self._carry_out(action)
        ack = {"action": action.key, "ok": refusal is None}
        if refusal:
            ack["error"] = refusal
        self._acks[action.key] = ack

    def wait_ack(self, key: str, timeout: float) -> dict | None:
        # send() has already produced the answer, so there is nothing to wait for.
        return self._acks.pop(key, None)

    def recv(self, timeout: float | None = None) -> Event | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def latest(self, key: str) -> Event | None:
        return self._latest.get(key)

    def boot_report(self, *, reset: bool = True, timeout: float = 5.0) -> dict | None:
        return None

    def close(self) -> None:
        pass

    # ---- the pretend firmware ----

    def _carry_out(self, action: Action) -> str | None:
        """Do what the firmware would do. Returns its reason for refusing, or None."""
        if self._load_contract().command(action.key) is None:
            return "unknown action"
        if action.key == "face":
            value = action.value if isinstance(action.value, dict) else {}
            if not value.get("text"):
                return "text missing"
            self.face = value
        return None

    # ---- things a person can do to the real robot ----

    def press(self, key: str) -> None:
        """Press and release a button."""
        self.emit(key, 1, "button")
        self.emit(key, 0, "button")

    def emit(self, key: str, value, event_type: str = "sensor") -> None:
        """Make the board send one event."""
        event = Event(type=event_type, key=key, value=value, received_at=time.time())
        self._latest[key] = event
        self._events.put(event)

    def unplug(self) -> None:
        """Pull the cable out."""
        self.unplugged = True

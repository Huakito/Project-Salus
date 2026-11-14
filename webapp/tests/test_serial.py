"""The real USB path: BoardManager over the Atech SDK's own serial transport.

There is no board in these tests. A pseudo-terminal stands in for the USB cable,
and a few lines of Python stand in for the firmware at the far end. That firmware
reads commands with a line-for-line copy of the parser the Atech SDK generates
(``atechJsonString`` and ``atechNum`` in atech/codegen.py), so these tests show
what a real board would make of what the app sends.
"""

import json
import os
import re
import select
import threading
import time

import pytest

pytest.importorskip("termios", reason="pseudo-terminals need macOS or Linux")

import pty  # noqa: E402
import tty  # noqa: E402

import serial  # noqa: E402
from atech.runtime import Action, encode_action  # noqa: E402

from salus import board  # noqa: E402
from salus.board import CONNECTED, BoardManager, CommandFailed, real_board  # noqa: E402


def json_string(text: str, field: str, start: int = 0) -> tuple[str, int]:
    """atechJsonString: the text stored under ``field``, and where it ended."""
    pattern = f'"{field}":'
    i = text.find(pattern, start)
    if i < 0:
        return "", 0
    i += len(pattern)
    while i < len(text) and text[i] == " ":
        i += 1
    if i >= len(text) or text[i] != '"':
        return "", 0
    out = []
    i += 1
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            i += 1
            out.append({"n": "\n", "t": "\t"}.get(text[i], text[i]))
        elif c == '"':
            break
        else:
            out.append(c)
        i += 1
    return "".join(out), i


def number(text: str, field: str, fallback: int) -> int:
    """atechNum: the whole number stored under ``field``."""
    pattern = f'"{field}":'
    i = text.find(pattern)
    if i < 0:
        return fallback
    i += len(pattern)
    while i < len(text) and text[i] in ' "':
        i += 1
    if i >= len(text) or not (text[i].isdigit() or text[i] == "-"):
        return fallback
    digits = re.match(r"-?\d*", text[i:]).group()
    return int(digits) if digits not in ("", "-") else 0


def as_the_firmware_reads(line: str) -> tuple[str, str]:
    """The action key and value the firmware takes from one line off the cable."""
    key, end = json_string(line, "action")
    value, _ = json_string(line, "value", end)
    return key, value


class Firmware(threading.Thread):
    """The far end of the cable: acknowledges "face", ignores "sulk", refuses the rest."""

    def __init__(self):
        super().__init__(daemon=True)
        self.cable, self._board_end = pty.openpty()
        tty.setraw(self._board_end)  # pass bytes through untouched, as USB does
        self.port = os.ttyname(self._board_end)
        self.face = None
        self._closing = threading.Event()
        self.start()

    def run(self):
        pending = b""
        while not self._closing.is_set():
            # Never sit in read(): on a Mac, closing a pseudo-terminal that a thread
            # is blocked reading waits for that read, which would hang the test run.
            if not select.select([self.cable], [], [], 0.05)[0]:
                continue
            try:
                chunk = os.read(self.cable, 4096)
            except OSError:
                return
            if not chunk:
                return
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                self.handle(line.decode())

    def handle(self, line: str):
        key, value = as_the_firmware_reads(line)
        if key == "face":
            self.face = (json_string(value, "text")[0], number(value, "colour", 0xFFFF))
            self.say({"type": "ack", "payload": {"action": key, "ok": True}})
        elif key != "sulk":
            self.say({"type": "ack", "payload": {"action": key, "ok": False, "error": "unknown action"}})

    def say(self, message: dict):
        os.write(self.cable, json.dumps(message, separators=(",", ":")).encode() + b"\n")

    def press(self, key: str):
        for value in (1, 0):
            self.say({"type": "event", "payload": {"event_type": "button", "key": key, "value": value}})

    def close(self):
        self._closing.set()
        self.join(2)
        os.close(self.cable)
        os.close(self._board_end)


@pytest.fixture
def firmware():
    firmware = Firmware()
    yield firmware
    firmware.close()


@pytest.fixture
def manager(firmware, monkeypatch):
    monkeypatch.setattr(board, "ACK_SECONDS", 0.3)  # so a silent board does not hold the tests up
    manager = BoardManager(real_board(firmware.port), mode="real", retry_seconds=0.05)
    manager.start()
    assert manager.wait_for(CONNECTED, 3)
    yield manager
    manager.stop()


def test_it_connects_to_the_port_it_was_given(manager, firmware):
    assert manager.status() == {
        "state": CONNECTED,
        "mode": "real",
        "port": firmware.port,
        "detail": "",
        "waiting": 0,
        "mic_muted": False,
        "volume": None,
        "knob": None,
    }


def test_a_face_arrives_just_as_it_was_sent(manager, firmware):
    art = ' /\\_/\\\n( "o.o" )\n > ^ <'  # backslashes, quotes and line breaks: all need escaping twice
    manager.send("face", {"text": art, "colour": 2016})
    assert firmware.face == (art, 2016)


def test_a_refusal_comes_back_with_the_firmwares_reason(manager):
    with pytest.raises(CommandFailed, match="unknown action"):
        manager.send("launch_rocket")


def test_silence_is_reported_and_the_connection_survives_it(manager, firmware):
    with pytest.raises(CommandFailed, match="No answer from the board"):
        manager.send("sulk")
    manager.send("face", {"text": "(^_^)", "colour": 1})
    assert firmware.face == ("(^_^)", 1)


def test_events_arrive_and_the_firmwares_log_lines_are_ignored(manager, firmware):
    os.write(firmware.cable, b"E (1234) speaker: a driver's log line, not JSON\n")
    firmware.press("yes_btn")

    def events():
        return [(e["type"], e["key"], e["value"]) for e in manager.recent_events()]

    deadline = time.monotonic() + 2
    while len(events()) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert events() == [("button", "yes_btn", 1), ("button", "yes_btn", 0)]


def test_disconnect_really_frees_the_port_for_another_program(manager, firmware):
    with pytest.raises(serial.SerialException):  # while the app holds it, nothing else can
        serial.Serial(firmware.port, exclusive=True)

    manager.disconnect()
    serial.Serial(firmware.port, exclusive=True).close()  # as `atech upload` would

    assert manager.connect()["state"] == CONNECTED
    manager.send("face", {"text": "(o_o)", "colour": 2})
    assert firmware.face == ("(o_o)", 2)


def test_why_only_plain_keyboard_characters_are_sent():
    # The reason for wire_problem() in board.py: the SDK escapes anything else as
    # \uXXXX, and the firmware's parser does not know that escape.
    line = encode_action(Action(key="say", value="café\r")).decode()
    assert as_the_firmware_reads(line) == ("say", "cafu00e9r")

"""BoardManager, run against fake boards so no hardware is needed."""

import threading
from dataclasses import dataclass, field

import pytest
from atech import Board, NoBoardFoundError
from atech.runtime import Action, encode_action

from salus.board import (
    CONNECTED,
    MAX_WAITING,
    RELEASED,
    SEARCHING,
    BoardManager,
    Busy,
    CommandFailed,
    InvalidCommand,
    Link,
    NotConnected,
    wire_problem,
)
from salus.contract import load_contract
from salus.fake_board import FakeBoardTransport

CONTRACT = load_contract()


class GatedTransport(FakeBoardTransport):
    """A fake board that can be made to sit on a command until the test lets it through."""

    def __init__(self, load):
        super().__init__(load)
        self.order = []  # the value of every command, in the order it arrived
        self.gate = threading.Event()
        self.gate.set()  # open: commands go straight through
        self.arrived = threading.Event()

    def send(self, action):
        self.order.append(action.value)
        self.arrived.set()
        self.gate.wait(5)
        super().send(action)


@dataclass
class Rig:
    """A running BoardManager, with a hand on the cable."""

    manager: BoardManager = None
    boards: list = field(default_factory=list)  # every fake board opened so far
    plugged_in: bool = True

    @property
    def board(self) -> GatedTransport:
        return self.boards[-1]

    def open_link(self) -> Link:
        if not self.plugged_in:
            raise NoBoardFoundError("no Atech board found on USB.")
        transport = GatedTransport(lambda: CONTRACT)
        self.boards.append(transport)
        return Link(Board(transport), "fake board", lambda: not transport.unplugged)


def make_rig(**timing) -> Rig:
    rig = Rig()
    rig.manager = BoardManager(rig.open_link, mode="fake", **timing)
    rig.manager.start()
    assert rig.manager.wait_for(CONNECTED, 2)
    return rig


@pytest.fixture
def rig():
    rig = make_rig(retry_seconds=0.02, presence_seconds=0.02)
    yield rig
    rig.manager.stop()


@pytest.fixture
def unwatched_rig():
    """A rig that does not check the cable, so only a failed write can reveal it is gone."""
    rig = make_rig(retry_seconds=0.02, presence_seconds=60)
    yield rig
    rig.manager.stop()


# ---- sending commands ----


def test_a_command_in_the_contract_is_acknowledged(rig):
    assert rig.manager.send("spk_stop") is None


def test_a_face_reaches_the_screen(rig):
    rig.manager.send("face", {"text": "(^_^)", "colour": 2016})
    assert rig.board.face == {"text": "(^_^)", "colour": 2016}


def test_a_command_the_firmware_does_not_know_is_refused(rig):
    with pytest.raises(CommandFailed, match="unknown action"):
        rig.manager.send("launch_rocket")


def test_a_refusal_carries_the_firmwares_reason(rig):
    with pytest.raises(CommandFailed, match="text missing"):
        rig.manager.send("face", {"colour": 2016})


def test_a_board_that_does_not_answer_is_reported(rig):
    rig.board.silent = True
    with pytest.raises(CommandFailed, match="No answer from the board"):
        rig.manager.send("spk_stop")
    assert rig.manager.status()["state"] == CONNECTED  # silence alone is not a lost connection


def test_commands_go_one_at_a_time_in_the_order_they_were_given(rig):
    waiting = [rig.manager.submit("spk_set_volume", step / 10) for step in range(10)]
    for command in waiting:
        command.result(timeout=2)
    assert rig.board.order == [step / 10 for step in range(10)]


def test_only_so_many_commands_may_wait(rig):
    rig.board.gate.clear()  # the board sits on the first command...
    first = rig.manager.submit("spk_stop")
    assert rig.board.arrived.wait(2)
    waiting = [rig.manager.submit("spk_stop") for _ in range(MAX_WAITING)]  # ...while the queue fills
    with pytest.raises(Busy, match="already waiting"):
        rig.manager.submit("spk_stop")
    assert rig.manager.status()["waiting"] == MAX_WAITING

    rig.board.gate.set()
    for command in [first, *waiting]:
        command.result(timeout=2)
    assert rig.manager.status()["waiting"] == 0


# ---- what cannot be sent at all ----


def test_text_the_firmware_would_garble_is_refused(rig):
    with pytest.raises(InvalidCommand, match='plain keyboard characters, and this contains "é"'):
        rig.manager.submit("spk_play_rtttl", "café")
    assert rig.board.order == []  # nothing was sent


def test_garbling_is_caught_inside_nested_values():
    assert "ʘ" in wire_problem("face", {"text": "(ʘ_ʘ)", "colour": 1})
    assert "ʘ" in wire_problem("thing", {"list": [1, ["ok", "ʘ"]]})
    assert "U+000D" in wire_problem("thing", "line\r\n")
    assert wire_problem("face", {"text": "line one\n\tline two", "colour": 1}) is None


def test_a_command_longer_than_the_firmwares_line_buffer_is_refused():
    def encoded_length(text):
        return len(encode_action(Action(key="k", value=text))) - 1

    fits, too_long = "x" * 4071, "x" * 4072
    assert (encoded_length(fits), encoded_length(too_long)) == (4096, 4097)
    assert wire_problem("k", fits) is None
    assert "4097 characters" in wire_problem("k", too_long)


def test_quotes_and_backslashes_count_for_more_once_encoded():
    # A 1500-character face is well under 4096, until each backslash is escaped twice over.
    assert wire_problem("face", {"text": "x" * 1500, "colour": 1}) is None
    assert "accepts at most 4096" in wire_problem("face", {"text": "\\" * 1500, "colour": 1})


# ---- connecting and disconnecting ----


def test_status_describes_the_connection(rig):
    assert rig.manager.status() == {
        "state": CONNECTED,
        "mode": "fake",
        "port": "fake board",
        "detail": "",
        "waiting": 0,
        "mic_muted": False,
        "volume": None,
    }


def test_disconnect_lets_go_of_the_port_and_connect_takes_it_back(rig):
    status = rig.manager.disconnect()
    assert status["state"] == RELEASED
    assert status["port"] is None
    assert "new firmware can be uploaded" in status["detail"]
    with pytest.raises(NotConnected, match="No board is connected"):
        rig.manager.submit("spk_stop")

    assert rig.manager.connect()["state"] == CONNECTED
    assert len(rig.boards) == 2  # it opened the board afresh
    rig.manager.send("spk_stop")


def test_disconnect_does_not_come_back_by_itself(rig):
    rig.manager.disconnect()
    assert not rig.manager.wait_for(CONNECTED, 0.2)  # several retry periods later, still released


def test_connect_reports_when_there_is_no_board_to_find(rig):
    rig.manager.disconnect()
    rig.plugged_in = False
    status = rig.manager.connect()
    assert status["state"] == SEARCHING
    assert status["detail"] == "No Atech board found on USB."


def test_pulling_the_cable_is_noticed_and_plugging_it_back_reconnects(rig):
    rig.plugged_in = False
    rig.board.unplug()
    assert rig.manager.wait_for(SEARCHING, 2)
    with pytest.raises(NotConnected):
        rig.manager.submit("spk_stop")

    rig.plugged_in = True
    assert rig.manager.wait_for(CONNECTED, 2)
    assert len(rig.boards) == 2
    rig.manager.send("spk_stop")


def test_a_failed_write_also_reveals_that_the_board_is_gone(unwatched_rig):
    rig = unwatched_rig
    rig.plugged_in = False
    rig.board.unplug()
    with pytest.raises(NotConnected, match="lost while sending"):
        rig.manager.send("spk_stop")
    assert rig.manager.wait_for(SEARCHING, 2)


def test_commands_still_waiting_fail_when_the_board_is_released(rig):
    rig.board.gate.clear()
    in_flight = rig.manager.submit("spk_stop")
    assert rig.board.arrived.wait(2)
    queued = [rig.manager.submit("spk_stop") for _ in range(3)]

    releasing = threading.Thread(target=rig.manager.disconnect)
    releasing.start()
    rig.board.gate.set()
    releasing.join(5)

    assert in_flight.result(timeout=2) is None  # the one already on the cable finishes
    for command in queued:
        with pytest.raises(NotConnected):
            command.result(timeout=2)
    assert rig.manager.status() == {**rig.manager.status(), "state": RELEASED, "waiting": 0}


def test_stopping_lets_go_of_the_board():
    rig = make_rig()
    rig.manager.stop()
    assert rig.manager.status()["state"] == RELEASED
    with pytest.raises(NotConnected):
        rig.manager.submit("spk_stop")


# ---- events from the board ----


def test_events_from_the_board_are_collected(rig):
    rig.board.press("yes_btn")
    rig.board.emit("mic_level", 412)

    def collected():
        return [(e["type"], e["key"], e["value"]) for e in rig.manager.recent_events()]

    for _ in range(100):
        if len(collected()) == 3:
            break
        threading.Event().wait(0.01)
    assert collected() == [("button", "yes_btn", 1), ("button", "yes_btn", 0), ("sensor", "mic_level", 412)]


def test_status_follows_the_microphone_mute_the_board_reports(rig):
    def muted_becomes(wanted):
        for _ in range(200):
            if rig.manager.status()["mic_muted"] is wanted:
                return True
            threading.Event().wait(0.01)
        return False

    assert rig.manager.status()["mic_muted"] is False  # nothing heard from the board yet
    rig.board.emit("mic_muted", 1, "state")
    assert muted_becomes(True)
    rig.board.emit("mic_muted", 0, "state")
    assert muted_becomes(False)

    # Once the board is gone, its last word is not repeated as if it were still true.
    rig.board.emit("mic_muted", 1, "state")
    assert muted_becomes(True)
    rig.manager.disconnect()
    assert rig.manager.status()["mic_muted"] is False


def test_status_follows_the_volume_the_board_reports(rig):
    def volume_becomes(wanted):
        for _ in range(200):
            if rig.manager.status()["volume"] == wanted:
                return True
            threading.Event().wait(0.01)
        return False

    assert rig.manager.status()["volume"] is None  # not known until the board says
    rig.board.emit("volume", 65, "state")
    assert volume_becomes(65)
    rig.board.emit("volume", 250, "state")  # nonsense is kept inside 0 to 100
    assert volume_becomes(100)
    rig.manager.disconnect()
    assert rig.manager.status()["volume"] is None

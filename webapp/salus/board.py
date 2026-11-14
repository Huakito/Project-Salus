"""The one connection to the robot's board.

Only one program can hold the board's USB port, and the board loses commands if
several arrive at once. So everything that touches the board happens on a single
worker thread here. It finds the board, sends the waiting commands one at a
time, and notices when the cable is pulled out.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

import serial
from atech import ActionError, Board, NoBoardFoundError, PortUnavailableError, discover_ports
from atech.runtime import Action, Event, all_ports, encode_action

from .contract import Contract
from .fake_board import FakeBoardTransport

log = logging.getLogger("salus.board")

CONNECTED = "connected"  # the port is open and commands can be sent
SEARCHING = "searching"  # no board yet; trying again every few seconds
RELEASED = "released"  # asked to let go of the port, so the board can be flashed

RELEASED_DETAIL = "The USB port is free, so new firmware can be uploaded. Press Connect when that is done."

MAX_WIRE_CHARS = 4096  # the firmware drops whatever comes after this many characters on one line
MAX_WAITING = 16  # commands that may wait for their turn
ACK_SECONDS = 2.0  # how long the board gets to confirm one command
TICK_SECONDS = 0.05  # how often the worker looks up from waiting for a command


class BoardError(Exception):
    """A command was not carried out. The message is fit to show to the person using the page."""


class NotConnected(BoardError):
    """There is no board to send to."""


class Busy(BoardError):
    """Too many commands are already waiting."""


class InvalidCommand(BoardError):
    """The command cannot be sent as it stands."""


class CommandFailed(BoardError):
    """The board refused the command, or did not answer."""


@dataclass
class Link:
    """An open connection to a board."""

    board: Board
    port: str
    is_present: Callable[[], bool]  # False once the cable has been pulled out


def real_board(port: str | None = None) -> Callable[[], Link]:
    """How to open the robot on USB. With no port given, the first board found is used."""

    def open_link() -> Link:
        device = port
        if device is None:
            found = discover_ports()
            if not found:
                raise NoBoardFoundError(
                    "No Atech board found on USB. Plug it in with a data cable "
                    "(a charge-only cable will not work)."
                )
            device = found[0].device
        board = Board.connect(port=device)

        def listed() -> bool:
            return any(p.device == device for p in all_ports())

        # A port given under a name the listing does not use (/dev/tty.* on a Mac) never
        # shows up there. For those, a failed write is what reveals that the board has gone.
        return Link(board, device, listed if listed() else lambda: True)

    return open_link


def fake_board(load_contract: Callable[[], Contract]) -> Callable[[], Link]:
    """How to open the pretend robot (see fake_board.py)."""

    def open_link() -> Link:
        transport = FakeBoardTransport(load_contract)
        return Link(Board(transport), "fake board", lambda: not transport.unplugged)

    return open_link


def wire_problem(key: str, value: Any) -> str | None:
    """Why this command cannot be sent as it stands, or None if it can.

    Both limits come from the firmware the Atech SDK generates. It reads plain
    ASCII only (an accented letter arrives as "u00e9"), and it cuts every line
    off at 4096 characters.
    """
    for text in (key, *_texts(value)):
        for ch in text:
            if not (" " <= ch <= "~" or ch in "\n\t"):
                shown = f'"{ch}"' if ch.isprintable() else f"a control character (U+{ord(ch):04X})"
                return f"The board can only receive plain keyboard characters, and this contains {shown}."
    length = len(encode_action(Action(key=key, value=value))) - 1  # not counting the newline that ends it
    if length > MAX_WIRE_CHARS:
        return (
            f"This is {length} characters long once encoded for the board, "
            f"which accepts at most {MAX_WIRE_CHARS} per command."
        )
    return None


def _texts(value: Any):
    """Every piece of text inside a value, however deeply it is nested."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for name, item in value.items():
            yield str(name)
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


def _sentence(error: Exception) -> str:
    """The SDK's error messages start in lower case; make one read as a sentence."""
    text = str(error)
    return text[:1].upper() + text[1:]


@dataclass
class _Job:
    key: str
    value: Any
    done: Future = field(default_factory=Future)


class BoardManager:
    """Keeps a board connected and sends it commands, one at a time.

    ``open_link`` opens a board, or raises NoBoardFoundError / PortUnavailableError
    when there is none to open yet: ``real_board()`` or ``fake_board()``.
    """

    def __init__(
        self,
        open_link: Callable[[], Link],
        *,
        mode: str,
        retry_seconds: float = 2.0,
        presence_seconds: float = 1.0,
    ) -> None:
        self.mode = mode
        self._open_link = open_link
        self._retry_seconds = retry_seconds  # between attempts to find a board
        self._presence_seconds = presence_seconds  # between checks that the cable is still in

        self._changed = threading.Condition()  # guards the five fields below
        self._state = SEARCHING
        self._port: str | None = None
        self._detail = "Looking for the board…"
        self._wanted = True  # False after disconnect(): leave the port alone
        self._events: deque[Event] = deque(maxlen=200)
        self._latest: dict[str, Any] = {}  # the last value the board sent for each event key

        self._jobs: queue.Queue[_Job] = queue.Queue(maxsize=MAX_WAITING)
        self._wake = threading.Event()  # cuts the worker's wait short when something has changed
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_presence_check = 0.0

    # ---- called from the web server ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="salus-board", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Let go of the board and end the worker thread."""
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=ACK_SECONDS + 3)

    def status(self) -> dict:
        with self._changed:
            return {
                "state": self._state,
                "mode": self.mode,
                "port": self._port,
                "detail": self._detail,
                "waiting": self._jobs.qsize(),
                # The firmware reports this itself (see mic_muted in commands.json).
                "mic_muted": self._latest.get("mic_muted") in (1, "1"),
                # Speaker volume as a percentage, or None until the board has said.
                "volume": self._volume(),
                # The knob's running count of clicks, or None until it has been turned.
                "knob": self._knob(),
            }

    def _volume(self) -> int | None:
        heard = self._latest.get("volume")
        if isinstance(heard, bool) or not isinstance(heard, (int, float)):
            return None
        return max(0, min(100, round(heard)))

    def _knob(self) -> int | None:
        heard = self._latest.get("knob")
        return heard if isinstance(heard, int) and not isinstance(heard, bool) else None

    def wait_for(self, state: str, timeout: float) -> bool:
        """Wait until the board is in ``state``. False if that did not happen in time."""
        with self._changed:
            return self._changed.wait_for(lambda: self._state == state, timeout=timeout)

    def connect(self) -> dict:
        """Start looking for the board again. Returns the status once the first attempt is over."""
        with self._changed:
            self._wanted = True
            self._wake.set()
            self._changed.wait_for(lambda: self._state != RELEASED, timeout=ACK_SECONDS + 3)
        return self.status()

    def disconnect(self) -> dict:
        """Let go of the USB port. Returns once it really is free, so flashing can start."""
        with self._changed:
            self._wanted = False
        self._wake.set()
        self.wait_for(RELEASED, timeout=ACK_SECONDS + 3)
        return self.status()

    def submit(self, key: str, value: Any = "") -> Future:
        """Queue one command. The Future finishes when the board has confirmed it,
        or fails with a BoardError."""
        if value is None:
            value = ""
        problem = wire_problem(key, value)
        if problem:
            raise InvalidCommand(problem)
        job = _Job(key, value)
        # Checking the state and joining the queue happen under one lock, so a command
        # can never slip into the queue after the worker has given up on the board.
        with self._changed:
            if self._state != CONNECTED:
                raise NotConnected(f"No board is connected. {self._detail}".strip())
            try:
                self._jobs.put_nowait(job)
            except queue.Full:
                raise Busy(f"The board is busy: {MAX_WAITING} commands are already waiting.") from None
        return job.done

    def send(self, key: str, value: Any = "") -> None:
        """Send one command and wait until the board has confirmed it.
        Raises a BoardError if it was not carried out."""
        try:
            self.submit(key, value).result(timeout=(MAX_WAITING + 2) * ACK_SECONDS)
        except FutureTimeout:
            raise CommandFailed("Gave up waiting for the board to take this command.") from None

    def recent_events(self) -> list[dict]:
        """The latest events the board sent (button presses, sensor readings), oldest first."""
        with self._changed:
            return [event.model_dump() for event in self._events]

    # ---- the worker thread: the only code that touches the board ----

    def _run(self) -> None:
        link: Link | None = None
        try:
            while not self._stopping.is_set():
                try:
                    link = self._serve(link) if link else self._try_open()
                except Exception:
                    # A bug here must not kill the only thread that can reach the board.
                    log.exception("unexpected error in the board worker")
                    if link is not None:
                        self._close(link)
                        link = None
                    self._leave(SEARCHING, "Something went wrong talking to the board. Trying again…")
                    self._pause(self._retry_seconds)
        finally:
            if link is not None:
                self._close(link)
            self._leave(RELEASED, "The app is shutting down.")

    def _try_open(self) -> Link | None:
        if not self._wanted:
            self._set(RELEASED, detail=RELEASED_DETAIL)
            self._pause(None)
            return None
        try:
            link = self._open_link()
        except (NoBoardFoundError, PortUnavailableError) as exc:
            self._set(SEARCHING, detail=_sentence(exc))
            self._pause(self._retry_seconds)
            return None
        self._next_presence_check = time.monotonic() + self._presence_seconds
        self._set(CONNECTED, port=link.port)
        return link

    def _serve(self, link: Link) -> Link | None:
        """One turn while connected. Returns the link, or None once it has ended."""
        if not self._wanted:
            self._close(link)
            self._leave(RELEASED, RELEASED_DETAIL)
            return None

        now = time.monotonic()
        if now >= self._next_presence_check:
            self._next_presence_check = now + self._presence_seconds
            if not link.is_present():
                self._close(link)
                self._leave(SEARCHING, "The board was unplugged. Looking for it…")
                return None

        try:
            job = self._jobs.get(timeout=TICK_SECONDS)
        except queue.Empty:
            job = None
        if job is not None and not self._carry_out(link, job):
            self._close(link)
            self._leave(SEARCHING, "The USB connection to the board was lost. Looking for it…")
            return None

        # The SDK keeps every event until someone takes it, so take them even
        # though nothing shows them yet (a microphone alone sends four a second).
        for _ in range(100):
            event = link.board.transport.recv(timeout=0)
            if event is None:
                break
            with self._changed:
                self._events.append(event)
                self._latest[event.key] = event.value
        return link

    def _carry_out(self, link: Link, job: _Job) -> bool:
        """Send one command and settle its Future. Returns False if the connection died."""
        if not job.done.set_running_or_notify_cancel():
            return True
        try:
            link.board.call(job.key, job.value, timeout=ACK_SECONDS)
        except ActionError as exc:
            # The board said no, or said nothing. The cable itself is fine.
            job.done.set_exception(CommandFailed(_sentence(exc)))
        except (serial.SerialException, OSError):
            # Writing to the port failed: the board is gone.
            job.done.set_exception(NotConnected("The USB connection to the board was lost while sending."))
            return False
        except Exception as exc:
            log.exception("unexpected error sending %r", job.key)
            job.done.set_exception(CommandFailed(f"Something went wrong sending this command: {exc}"))
        else:
            job.done.set_result(None)
        return True

    def _leave(self, state: str, detail: str) -> None:
        """Record that there is no board any more, and fail whatever was still waiting for it."""
        self._set(state, detail=detail)  # from here on, submit() turns new commands away
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if job.done.set_running_or_notify_cancel():
                job.done.set_exception(NotConnected(f"No board is connected. {detail}"))

    def _close(self, link: Link) -> None:
        try:
            link.board.close()
        except Exception:
            log.debug("error closing the board", exc_info=True)

    def _set(self, state: str, *, port: str | None = None, detail: str = "") -> None:
        with self._changed:
            if (state, port, detail) == (self._state, self._port, self._detail):
                return
            self._state, self._port, self._detail = state, port, detail
            if state != CONNECTED:
                self._latest.clear()  # what the board last said is not known to be true any more
            self._changed.notify_all()
        log.info("board %s%s%s", state, f" on {port}" if port else "", f": {detail}" if detail else "")

    def _pause(self, seconds: float | None) -> None:
        """Wait, but wake at once if connect(), disconnect() or stop() is called."""
        self._wake.wait(seconds)
        self._wake.clear()

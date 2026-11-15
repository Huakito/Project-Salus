"""The web server: a few JSON endpoints in front of the board, and the page itself."""

from __future__ import annotations

import logging
import os
import json
import threading
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .board import (
    BoardError,
    BoardManager,
    Busy,
    CommandFailed,
    InvalidCommand,
    NotConnected,
    fake_board,
    real_board,
)
from .calendar import DemoCalendar
from .contract import DEFAULT_PATH, Contract, ContractError, load_contract
from .face import FaceError, check_face, rgb565
from .feedback import FeedbackService
from .memory import HistoryStore
from .journal import JournalStore
from .voice import ElevenLabsVoice, MAX_SPEECH_CHARS, VoiceError
from .wifi import WifiRequest, this_computer

log = logging.getLogger("salus")

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

# What the browser is told when a command is not carried out.
HTTP_STATUS = {
    InvalidCommand: 422,  # the request itself is wrong
    NotConnected: 409,  # right request, but there is no board at the moment
    Busy: 503,  # try again shortly
    CommandFailed: 502,  # the board refused, or did not answer
}


class CommandRequest(BaseModel):
    key: str
    value: Any = None


class FaceRequest(BaseModel):
    text: str = Field(max_length=4000)
    colour: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")


class MovementCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=300)
    answer: str = Field(min_length=1, max_length=1000)
    response: str = Field(min_length=1, max_length=500)
    response_source: Literal["openai", "local", "scripted_safety"]
    safety_pause: bool = False


class MovementSession(BaseModel):
    """The compact result of a camera game. Camera images are never accepted."""

    model_config = ConfigDict(extra="forbid")

    game: Literal["salus_says", "hand_signals"]
    mode: Literal["standing", "seated", "hands"]
    duration_seconds: int = Field(ge=1, le=3600)
    movements_offered: list[str] = Field(min_length=1, max_length=30)
    movements_completed: list[str] = Field(max_length=30)
    movements_skipped: list[str] = Field(max_length=30)
    tracking_quality: float = Field(ge=0, le=1)
    feeling: Literal["not_for_me", "okay", "good"] | None = None
    check_ins: list[MovementCheckIn] = Field(default_factory=list, max_length=5)


class CheckInRequest(BaseModel):
    """A transcript only. Audio is never accepted by this server."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=300)
    answer: str = Field(min_length=1, max_length=1000)
    question_id: str | None = Field(default=None, max_length=80)


class VoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_SPEECH_CHARS)


class JournalEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=5000)


class JournalChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=1000)


class ConversationContinueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quiet_turn: int = Field(default=0, ge=0, le=20)


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spoken_guidance: bool | None = None
    spoken_check_ins: bool | None = None
    robot_colour: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class _HideStatusPolls(logging.Filter):
    """The page asks for /api/status every second or two; keep that out of the terminal."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (isinstance(args, tuple) and len(args) == 5 and args[2] in ("/api/status", "/api/events") and args[4] == 200)


def _log_to_terminal() -> None:
    """Show this app's own log lines (board connected, board unplugged...) next to uvicorn's."""
    logger = logging.getLogger("salus")
    if logger.handlers:
        return  # already set up
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logging.getLogger("uvicorn.access").addFilter(_HideStatusPolls())


def create_app(
    *,
    mode: str = "real",
    board_port: str | None = None,
    contract_path: Path = DEFAULT_PATH,
    data_dir: Path | None = None,
    feedback_service: FeedbackService | None = None,
    voice_service: ElevenLabsVoice | None = None,
) -> FastAPI:
    """Build the app. ``mode`` is "real" (the robot on USB) or "fake" (no hardware needed)."""
    if mode not in ("real", "fake"):
        raise ValueError(f'mode must be "real" or "fake", not {mode!r}')

    _log_to_terminal()

    def contract() -> Contract:
        return load_contract(contract_path)

    if mode == "fake":
        log.warning("Using the FAKE board: nothing is sent to a real robot.")
    manager = BoardManager(fake_board(contract) if mode == "fake" else real_board(board_port), mode=mode)
    session_dir = data_dir or STATIC_DIR.parent / "data"
    session_path = session_dir / "movement-sessions.jsonl"
    session_lock = threading.Lock()
    feedback_service = feedback_service or FeedbackService.from_environment()
    reload_voice_environment = voice_service is None
    voice_service = voice_service or ElevenLabsVoice.from_environment()

    def current_voice_service():
        """Reload editable .env voice settings without requiring a restart."""
        if reload_voice_environment:
            return ElevenLabsVoice.from_environment()
        return voice_service
    history = HistoryStore(session_dir, Path(__file__).with_name("conversation_library.json"))
    calendar = DemoCalendar(session_dir)
    journal = JournalStore(session_dir)
    screen_lock = threading.Lock()
    screen_state: dict[str, Any] = {
        "kind": "face",
        "text": "(^_^)",
        "colour": "#00ff00",
        "volume": 40,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    def update_screen(**changes: Any) -> None:
        with screen_lock:
            screen_state.update(changes, updated_at=datetime.now(timezone.utc).isoformat())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.start()
        yield
        manager.stop()  # lets go of the USB port

    # The interactive /docs page is switched off: it loads its scripts from the
    # internet, and this app is meant to work without it.
    app = FastAPI(title="Salus", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager

    # ---- only this computer, and only our own page ----
    # The server listens on 127.0.0.1, but a web page on any site the person visits
    # can still make their browser send requests here. Two checks stop that:
    #  - the Host header must name this computer, which defeats a site that points
    #    its own domain name at 127.0.0.1;
    #  - a request that changes something must come from our own page. Browsers
    #    label a request made by another site with that site's Origin.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def own_page_only(request: Request, call_next):
        origin = request.headers.get("origin")
        changes_something = request.method not in ("GET", "HEAD", "OPTIONS")
        if changes_something and origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Requests from other websites are not accepted."}, status_code=403)
        response = await call_next(request)
        # The page loads nothing from anywhere else, so say so; and always check
        # for a newer copy of the page's files instead of using a stale one.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; "
            "media-src 'self' blob:; frame-ancestors 'none'"
        )
        response.headers["Permissions-Policy"] = "camera=(self), microphone=(self)"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-cache"
        return response

    # ---- errors, as {"detail": "a sentence the page can show"} ----

    @app.exception_handler(BoardError)
    async def board_error(request: Request, exc: BoardError):
        return JSONResponse({"detail": str(exc)}, status_code=HTTP_STATUS.get(type(exc), 500))

    @app.exception_handler(ContractError)
    async def contract_error(request: Request, exc: ContractError):
        return JSONResponse({"detail": str(exc)}, status_code=500)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        problems = "; ".join(f"{error['loc'][-1]}: {error['msg']}" for error in exc.errors())
        return JSONResponse({"detail": f"The request was not understood ({problems})."}, status_code=422)

    # ---- the API ----
    # These are plain (not async) functions on purpose: they wait for the board,
    # and FastAPI runs plain functions on spare threads so the server stays responsive.

    @app.get("/api/status")
    def status() -> dict:
        return manager.status()

    @app.post("/api/connect")
    def connect() -> dict:
        return manager.connect()

    @app.post("/api/disconnect")
    def disconnect() -> dict:
        return manager.disconnect()

    @app.get("/api/commands")
    def commands() -> dict:
        return contract().model_dump(exclude_none=True)

    @app.post("/api/command")
    def command(request: CommandRequest) -> dict:
        spec = contract().command(request.key)
        if spec is None:
            raise InvalidCommand(f'"{request.key}" is not a command in {contract_path.name}.')
        if spec.key == "wifi_set":
            # Only through /api/wifi, which checks the network name, password and address.
            raise InvalidCommand("Set the WiFi with the WiFi module (POST /api/wifi).")
        try:
            value = spec.check(request.value)
        except ValueError as exc:
            raise InvalidCommand(str(exc)) from None
        manager.send(spec.key, value)
        if spec.key == "spk_set_volume" and isinstance(value, (int, float)):
            update_screen(kind="volume", volume=round(float(value) * 100))
        return {"ok": True}

    @app.post("/api/face")
    def face(request: FaceRequest) -> dict:
        current = contract()
        if current.command("face") is None:
            raise InvalidCommand(f'There is no "face" command in {contract_path.name}.')
        try:
            lines = check_face(request.text, current.screen.width, current.screen.height)
        except FaceError as exc:
            raise InvalidCommand(str(exc)) from None
        value = {"text": "\n".join(lines), "colour": rgb565(request.colour)}
        manager.send("face", value)
        update_screen(kind="face", text=value["text"], colour=request.colour)
        return {"ok": True, **value}

    @app.get("/api/wifi")
    def wifi_details() -> dict:
        """What the WiFi form fills itself in with: this computer's address and network name."""
        return {"computer": this_computer(), "robot": manager.status()["wifi"]}

    @app.post("/api/wifi")
    def set_wifi(request: WifiRequest) -> dict:
        """Send the network and the laptop's address to the board. The password is not kept or logged."""
        if contract().command("wifi_set") is None:
            raise InvalidCommand(f'There is no "wifi_set" command in {contract_path.name}.')
        manager.send("wifi_set", request.for_board())
        return {"ok": True, "ssid": request.ssid, "laptop": request.laptop}

    @app.get("/api/screen-state")
    def get_screen_state() -> dict:
        """The most recent screen scene requested through this app, for the detached preview."""
        with screen_lock:
            return dict(screen_state)

    @app.get("/api/events")
    def events() -> list[dict]:
        return manager.recent_events()

    @app.post("/api/movement-sessions")
    def save_movement_session(session: MovementSession) -> dict:
        """Save derived game results locally. The endpoint has no video field by design."""
        session_data = session.model_dump()
        feedback = feedback_service.movement_feedback(session_data)
        record = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **session_data,
            "feedback": feedback.message,
            "feedback_source": feedback.source,
        }
        session_dir.mkdir(parents=True, exist_ok=True)
        with session_lock, session_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, separators=(",", ":")) + "\n")

        return {
            "ok": True,
            "feedback": feedback.message,
            "feedback_source": feedback.source,
            "recorded_at": record["recorded_at"],
        }

    @app.post("/api/check-in")
    def answer_check_in(request: CheckInRequest) -> dict:
        """Reply to a typed or browser-transcribed answer. No audio reaches the server."""
        reply = feedback_service.check_in_reply(request.question, request.answer, history.context())
        history.record(question_id=request.question_id, question=request.question, answer=request.answer,
                       response=reply.message, response_source=reply.source,
                       safety_pause=reply.safety_pause, memory_note=reply.memory_note)
        return {
            "ok": True,
            "response": reply.message,
            "response_source": reply.source,
            "safety_pause": reply.safety_pause,
            "follow_up": reply.follow_up,
        }

    @app.get("/api/check-in/question")
    def next_check_in_question() -> dict:
        return history.next_question(calendar.question())

    @app.get("/api/calendar/events")
    def calendar_events() -> dict:
        return {**calendar.status(), "events": calendar.upcoming()}

    @app.get("/api/calendar/google/connect")
    def connect_google_calendar():
        try:
            return RedirectResponse(calendar.auth_url())
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/calendar/google/callback")
    def google_calendar_callback(code: str, state: str):
        try:
            calendar.finish_auth(code, state)
        except Exception as exc:
            return RedirectResponse("/?calendar=error#journal-view")
        return RedirectResponse("/?calendar=connected#journal-view")

    @app.post("/api/calendar/google/disconnect")
    def disconnect_google_calendar() -> dict:
        calendar.disconnect()
        return {"ok": True}

    @app.get("/api/journal")
    def journal_entries() -> dict:
        return {"entries": journal.entries()}

    @app.post("/api/journal")
    def add_journal_entry(request: JournalEntryRequest) -> dict:
        return {"ok": True, "entry": journal.add(request.body)}

    @app.post("/api/journal/chat")
    def journal_chat(request: JournalChatRequest) -> dict:
        message = " ".join(request.message.split())
        event = None
        # Demo grammar: "add/book/schedule <title> tomorrow [at 3pm]".
        match = re.search(
            r"(?:add|book|schedule)\s+(.+?)\s+(today|tomorrow)(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?[.!]?$",
            message, re.IGNORECASE,
        )
        if match:
            title, day_word, hour_text, minute_text, meridiem = match.groups()
            hour = int(hour_text or 9)
            if meridiem:
                hour = hour % 12 + (12 if meridiem.casefold() == "pm" else 0)
            start = datetime.now().astimezone().replace(hour=hour, minute=int(minute_text or 0), second=0, microsecond=0)
            if day_word.casefold() == "tomorrow":
                start += timedelta(days=1)
            event = calendar.add(title.strip(), start)
            response = f"Done — I added {event['title']} to your calendar for {start.strftime('%A at %-I:%M %p')}."
            follow_up = ""
            source = "calendar_action"
        else:
            context = history.context() + "\n\nJournal entries:\n" + journal.context()
            reply = feedback_service.check_in_reply("The person is talking with you about their journal.", message, context)
            response, follow_up, source = reply.message, reply.follow_up, reply.source
        return {"ok": True, "response": response, "follow_up": follow_up, "source": source, "calendar_event": event}

    @app.post("/api/journal/chat/continue")
    def continue_journal_chat(request: ConversationContinueRequest) -> dict:
        events = calendar.upcoming(5)
        calendar_context = "\n".join(
            f"- {event['starts_at']}: {event['title']}" for event in events
        ) or "No upcoming events."
        context = (
            history.context() + "\n\nJournal entries:\n" + journal.context()
            + "\n\nUpcoming calendar events:\n" + calendar_context
        )
        reply = feedback_service.conversation_continuation(context, request.quiet_turn)
        return {"ok": True, "response": reply.message, "source": reply.source}

    @app.get("/api/voice/status")
    def voice_status() -> dict:
        active_voice = current_voice_service()
        return {
            "provider": "elevenlabs" if active_voice.configured else "browser",
            "configured": active_voice.configured,
            "voice_id": active_voice.voice_id if active_voice.configured else None,
        }

    @app.post("/api/voice")
    def generate_voice(request: VoiceRequest):
        try:
            audio = current_voice_service().synthesize(request.text)
        except VoiceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        return Response(content=audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/settings")
    def get_settings() -> dict:
        return history.settings()

    @app.post("/api/settings")
    def save_settings(request: SettingsRequest) -> dict:
        return history.save_settings(request.model_dump(exclude_none=True))

    # Everything else is the page. Mounted last so it cannot shadow the API.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="page")
    return app


# What `uvicorn salus.main:app` and `python -m salus` run.
app = create_app(
    mode=os.environ.get("SALUS_BOARD", "real"),
    board_port=os.environ.get("SALUS_BOARD_PORT") or None,
)

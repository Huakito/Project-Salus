"""The web server: a few JSON endpoints in front of the board, and the page itself."""

from __future__ import annotations

import logging
import os
import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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
from .contract import DEFAULT_PATH, Contract, ContractError, load_contract
from .face import FaceError, check_face, rgb565
from .feedback import FeedbackService
from .memory import HistoryStore
from .garden import GardenStore

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


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spoken_guidance: bool | None = None
    spoken_check_ins: bool | None = None
    robot_colour: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class AddPlantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    species: str = Field(min_length=1, max_length=40)
    nickname: str = Field(default="", max_length=30)
    plot_slot: int | None = Field(default=None, ge=0, lt=16)


class MovePlantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plot_slot: int = Field(ge=0, lt=16)


class GardenAdminRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["advance_day", "grow_all", "make_due", "flower_all"]
    days: int = Field(default=1, ge=1, le=30)


class GardenCareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    activity: Literal["water", "mist", "turn", "sun", "check"]
    duration_seconds: int = Field(default=0, ge=0, le=600)
    score: float = Field(default=1, ge=0, le=1)


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
    history = HistoryStore(session_dir, Path(__file__).with_name("conversation_library.json"))
    garden = GardenStore(session_dir)
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
            "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; frame-ancestors 'none'"
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
        }

    @app.get("/api/check-in/question")
    def next_check_in_question() -> dict:
        return history.next_question()

    @app.get("/api/settings")
    def get_settings() -> dict:
        return history.settings()

    @app.post("/api/settings")
    def save_settings(request: SettingsRequest) -> dict:
        return history.save_settings(request.model_dump(exclude_none=True))

    @app.get("/api/garden")
    def garden_state() -> dict:
        return garden.state()

    @app.post("/api/garden/plants")
    def add_garden_plant(request: AddPlantRequest) -> dict:
        try:
            return {"ok": True, "plant": garden.add(request.species, request.nickname, request.plot_slot)}
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.post("/api/garden/plants/{plant_id}/move")
    def move_garden_plant(plant_id: int, request: MovePlantRequest) -> dict:
        try:
            return {"ok": True, "plant": garden.move(plant_id, request.plot_slot)}
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.post("/api/garden/plants/{plant_id}/care")
    def care_for_garden_plant(plant_id: int, request: GardenCareRequest) -> dict:
        try:
            plant, changed = garden.care(plant_id, request.activity, request.duration_seconds, request.score)
            return {"ok": True, "changed": changed, "plant": plant}
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.post("/api/garden/admin")
    def garden_admin(request: GardenAdminRequest) -> dict:
        try:
            return garden.admin(request.action, request.days)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)

    # Everything else is the page. Mounted last so it cannot shadow the API.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="page")
    return app


# What `uvicorn salus.main:app` and `python -m salus` run.
app = create_app(
    mode=os.environ.get("SALUS_BOARD", "real"),
    board_port=os.environ.get("SALUS_BOARD_PORT") or None,
)

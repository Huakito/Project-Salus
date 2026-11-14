"""
Laptop side of the Salus robot: live audio in, spoken replies out, over WiFi.

The robot connects to this computer on TCP port 9000 and keeps the connection
open. Both directions carry frames:

    type (1 byte) | payload length (2 bytes, little-endian) | payload

    robot -> laptop   A  mic audio    16 kHz, 16-bit signed little-endian, mono
                      M  mic muted    payload 1 = muted, 0 = on
                      D  done         the reply finished playing; it listens again
    laptop -> robot   T  thinking     the robot stops sending audio, shows a thinking face
                      A  reply audio  same format as the mic
                      E  end          the robot plays out the reply, then listens again
                      S  stop         the robot drops the reply and listens again

The robot's half of this is in project.yaml.

This file finds where each utterance starts and stops (by loudness), saves it
as a WAV, and replies: Whisper turns the speech into text, Gemini writes the
answer, Piper speaks it (see chat.py and firmware/.env.example). The reply is
spoken sentence by sentence, so the robot starts talking after the first one
while the rest are still being synthesised. Each connected robot keeps its own
short conversation memory, in this process only. If the reply cannot be made,
the robot says a short apology instead (when its audio is ready), and it always
gets an E so it listens again, well before its own 45 s thinking limit.

Run, with the project's venv activated:
    python firmware/server.py

The robot reaches port 9000 on every network interface, so the firewall has
to let it in. The /utterances page stays on this computer (127.0.0.1:8001).
"""

import array
import asyncio
import math
import sys
import time
import wave
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

import chat   # firmware/chat.py: the script's folder is on sys.path (tests add it too)

AUDIO_PORT = 9000          # LAPTOP_PORT in project.yaml
HTTP_PORT = 8001           # the web app already uses 8000
SAMPLE_RATE = 16_000       # the microphone and the speaker both run at 16 kHz
SAMPLE_WIDTH = 2           # 16-bit samples
RECORDINGS = Path(__file__).resolve().parent / "recordings"   # kept out of git by .gitignore

# Finding utterances. Loudness is RMS, about the units `atech monitor --key mic_level` shows.
CHUNK_MS = 30
START_RATIO = 3.0          # this many times louder than the room counts as speech...
MIN_LOUDNESS = 60          # ...and never quieter than this
END_SILENCE_MS = 800       # this long quiet ends an utterance
PRE_ROLL_MS = 300          # keep this much from before the start, so the first syllable survives
MIN_SPEECH_MS = 400        # less speech than this is a cough or a knock: ignored
MAX_UTTERANCE_S = 15

REPLY_CHUNK = 4096         # bytes of reply audio per frame (128 ms)
MAX_UTTERANCES = 100       # the log holds the conversation text, so keep only the latest
REPLY_DEADLINE_S = 40      # T until the first reply audio; the robot gives up after 45 s

utterances: list[dict] = []          # simple in-memory log for now
robots: set[asyncio.StreamWriter] = set()


@dataclass
class Pipeline:
    """The three blocking steps of a reply. Always call them with asyncio.to_thread."""

    transcribe: Callable[[bytes], str]              # 16 kHz PCM -> text ("" for no speech)
    respond: Callable[[list[dict], str], str]       # earlier turns, text -> reply text
    speak: Callable[[str], bytes]                   # text -> 16 kHz PCM
    problems: list[str] = field(default_factory=list)   # setup problems to tell the user about


pipeline: Pipeline | None = None     # built in the lifespan; tests put fakes here
fallback_audio: bytes | None = None  # chat.FALLBACK_LINE, spoken when the reply fails


def build_pipeline() -> Pipeline:
    config = chat.load_config()
    problems = []
    if not config.gemini_api_key:
        problems.append("GEMINI_API_KEY is not set: copy firmware/.env.example to firmware/.env and add your key")
    return Pipeline(
        transcribe=chat.Transcriber(config.whisper_model).transcribe,
        respond=chat.Chatter(config.gemini_api_key, config.gemini_model).respond,
        speak=chat.Speaker(config.voice, config.models_dir).speak,
        problems=problems,
    )


async def warm_up(pipe: Pipeline) -> None:
    """Load Whisper and Piper now, so the first utterance is not slow, and keep the
    apology as audio. A problem is printed with its fix; it never stops the server."""
    for problem in getattr(pipe, "problems", []):   # fakes in tests may not have any
        print(f"[chat] {problem}")

    async def load_whisper() -> None:
        try:
            await asyncio.to_thread(pipe.transcribe, bytes(SAMPLE_RATE * SAMPLE_WIDTH))   # 1 s of silence
        except Exception as error:
            print(f"[chat] Whisper did not load ({type(error).__name__}: {error}): "
                  "run pip install -r firmware/requirements.txt; the first run also downloads the model")

    async def load_piper() -> None:
        global fallback_audio
        try:
            fallback_audio = await asyncio.to_thread(pipe.speak, chat.FALLBACK_LINE) or None
        except FileNotFoundError as error:   # the message already says how to download the voice
            print(f"[chat] {error}")
        except Exception as error:
            print(f"[chat] Piper did not load ({type(error).__name__}: {error}): "
                  "run pip install -r firmware/requirements.txt")

    await asyncio.gather(load_whisper(), load_piper())
    print("[chat] warm-up finished")


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + len(payload).to_bytes(2, "little") + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    head = await reader.readexactly(3)
    size = int.from_bytes(head[1:], "little")
    return head[:1], (await reader.readexactly(size) if size else b"")


def to_samples(pcm: bytes) -> array.array:
    samples = array.array("h", pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def loudness(pcm: bytes) -> float:
    samples = to_samples(pcm)
    if not samples:
        return 0.0
    mean = sum(samples) / len(samples)
    return math.sqrt(sum((s - mean) ** 2 for s in samples) / len(samples))


class UtteranceFinder:
    """Cuts the robot's live audio into utterances, by loudness."""

    def __init__(self) -> None:
        self.chunk_bytes = SAMPLE_RATE * SAMPLE_WIDTH * CHUNK_MS // 1000
        self.background: float | None = None   # the room's own loudness
        self.reset()

    def reset(self) -> None:
        self.pending = bytearray()
        self.before: deque[bytes] = deque(maxlen=PRE_ROLL_MS // CHUNK_MS)
        self.speech: bytearray | None = None
        self.voiced_ms = 0
        self.quiet_ms = 0

    def feed(self, pcm: bytes) -> list[bytes]:
        """Add audio; return the utterances that just ended."""
        self.pending += pcm
        finished = []
        while len(self.pending) >= self.chunk_bytes:
            chunk = bytes(self.pending[: self.chunk_bytes])
            del self.pending[: self.chunk_bytes]
            level = loudness(chunk)
            # The room's loudness is the quiet between words: follow it down at
            # once, and up only slowly (about 15 s), so speech barely moves it.
            if self.background is None:
                self.background = level
            elif level < self.background:
                self.background += 0.3 * (level - self.background)
            else:
                self.background += 0.002 * (level - self.background)
            loud = level > max(MIN_LOUDNESS, self.background * START_RATIO)

            if self.speech is None:
                if loud:
                    self.speech = bytearray(b"".join(self.before) + chunk)
                    self.before.clear()
                    self.voiced_ms, self.quiet_ms = CHUNK_MS, 0
                else:
                    self.before.append(chunk)
                continue

            self.speech += chunk
            if loud:
                self.voiced_ms += CHUNK_MS
                self.quiet_ms = 0
            else:
                self.quiet_ms += CHUNK_MS
            too_long = len(self.speech) >= MAX_UTTERANCE_S * SAMPLE_RATE * SAMPLE_WIDTH
            if self.quiet_ms >= END_SILENCE_MS or too_long:
                if self.voiced_ms >= MIN_SPEECH_MS:
                    finished.append(bytes(self.speech))
                self.speech = None
        return finished


def save_wav(pcm: bytes) -> Path:
    path = RECORDINGS / f"utt_{datetime.now():%Y%m%d_%H%M%S_%f}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return path


class Robot:
    """One connected robot, and whether it is waiting on us."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.busy = False      # from an utterance until the robot says D
        self.reply_task: asyncio.Task | None = None
        self.history: list[dict] = []   # this conversation so far: {"role": "user"|"model", "text": ...}

    async def send(self, kind: bytes, payload: bytes = b"") -> None:
        self.writer.write(frame(kind, payload))
        await self.writer.drain()   # the robot reads only as fast as it plays: this paces the reply


@dataclass
class Turn:
    """What happened so far in one reply."""

    entry: dict                                # this utterance's line in `utterances`
    spoke: bool = False                        # some reply audio (or the apology) reached the robot
    failed: bool = False                       # the reply could not be made: apologise
    deadline: asyncio.Timeout | None = None    # armed until the first audio is sent


async def send_audio(robot: Robot, turn: Turn, pcm: bytes) -> None:
    pcm = pcm[: len(pcm) // 2 * 2]   # whole samples only
    for start in range(0, len(pcm), REPLY_CHUNK):
        await robot.send(b"A", pcm[start : start + REPLY_CHUNK])
        if not turn.spoke:
            turn.spoke = True
            if turn.deadline:   # the robot now waits only 10 s between chunks, not 45 s
                turn.deadline.reschedule(None)


async def synthesize(sentences: list[str], queue: asyncio.Queue) -> None:
    """Producer: speak each sentence in a worker thread and queue its audio, then None.
    It stops at the first sentence that fails (a broken voice fails every time)."""
    for sentence in sentences:
        try:
            audio = await asyncio.to_thread(pipeline.speak, sentence)
        except Exception as error:
            print(f"[tts] failed: {type(error).__name__}: {error}")
            break
        await queue.put(audio)
    await queue.put(None)   # not reached if cancelled, and then nobody is waiting for it


async def speak_sentences(robot: Robot, turn: Turn, sentences: list[str]) -> None:
    """Send the reply as it is synthesised: the robot starts after the first sentence,
    and the later ones are made while the earlier ones play (the queue holds 2)."""
    started = time.monotonic()
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    producer = asyncio.create_task(synthesize(sentences, queue))
    try:
        first = True
        while (audio := await queue.get()) is not None:
            if first:
                print(f"[reply] tts first chunk {time.monotonic() - started:.2f} s")
                first = False
            await send_audio(robot, turn, audio)
    finally:   # also runs when the reply is cancelled: leave no task behind
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


async def converse(robot: Robot, pcm: bytes, turn: Turn) -> None:
    """Everything between T and E: save, transcribe, ask, speak. Sets turn.failed when
    the robot should be given the apology."""
    entry = turn.entry
    wav_path = await asyncio.to_thread(save_wav, pcm)
    print(f"[audio] {wav_path.name}: {entry['seconds']:.1f} s")
    entry["file"] = str(wav_path)
    utterances.append(entry)
    del utterances[:-MAX_UTTERANCES]

    started = time.monotonic()
    try:
        if pipeline is None:
            raise RuntimeError("the voice pipeline did not start")
        heard = (await asyncio.to_thread(pipeline.transcribe, pcm)).strip()
    except Exception as error:
        print(f"[stt] failed: {type(error).__name__}: {error}")
        turn.failed = True
        return
    print(f"[stt] {time.monotonic() - started:.2f} s")
    if not heard:
        print("[stt] no speech heard")
        return
    entry["heard"] = heard

    # TODO(crisis): deterministic crisis screening MUST run on `heard` here, BEFORE respond()
    # is called (CLAUDE.md invariant 2). Not implemented yet: a self-harm signal must get a
    # scripted escalation to a person or crisis line, never an LLM-improvised answer.

    started = time.monotonic()
    try:
        said = chat.clean_for_speech(await asyncio.to_thread(pipeline.respond, list(robot.history), heard))
        sentences = chat.split_sentences(said)
        if not sentences:
            raise RuntimeError("the reply had nothing to say aloud")
    except Exception as error:   # never log the transcript or the key
        print(f"[llm] failed: {type(error).__name__}: {error}")
        turn.failed = True
        return
    print(f"[llm] {time.monotonic() - started:.2f} s, {len(sentences)} sentence(s)")
    entry["said"] = said
    await speak_sentences(robot, turn, sentences)
    if turn.spoke:   # remembered only once the robot has really been told, never when cancelled
        robot.history = chat.trim_history(
            [*robot.history, {"role": "user", "text": heard}, {"role": "model", "text": said}]
        )
    else:
        entry["said"] = ""
        turn.failed = True


async def reply(robot: Robot, pcm: bytes) -> None:
    seconds = len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)
    turn = Turn({"file": "", "seconds": round(seconds, 2), "heard": "", "said": ""})
    cancelled = False
    try:
        await robot.send(b"T")
        try:
            async with asyncio.timeout(REPLY_DEADLINE_S) as deadline:
                turn.deadline = deadline
                await converse(robot, pcm, turn)
        except TimeoutError:
            print(f"[reply] gave up after {REPLY_DEADLINE_S} s")
            turn.failed = True
        except Exception as error:   # say why, and still let the robot listen again
            print(f"[reply] failed: {type(error).__name__}: {error}")
            turn.failed = True
        finally:
            turn.deadline = None
        if turn.failed and not turn.spoke and fallback_audio:   # the apology is said at most once
            turn.entry["said"] = chat.FALLBACK_LINE
            await send_audio(robot, turn, fallback_audio)
    except asyncio.CancelledError:   # the robot sent D, or it disconnected: it has moved on, so no E
        cancelled = True
        raise
    except Exception as error:
        print(f"[reply] failed: {type(error).__name__}: {error}")
    finally:
        if not cancelled and not robot.writer.is_closing():
            try:
                await robot.send(b"E")
            except (ConnectionError, OSError):
                pass


async def stop_reply(robot: Robot) -> None:
    """Cancel the reply in progress, if any, and wait until it is gone."""
    task, robot.reply_task = robot.reply_task, None
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def on_robot(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")[0]
    print(f"[robot] connected from {peer}")
    for old in [w for w in robots if w.get_extra_info("peername")[0] == peer]:
        old.close()   # the robot restarted or lost its link: that connection is dead
    robots.add(writer)
    robot = Robot(writer)
    finder = UtteranceFinder()
    try:
        while True:
            kind, payload = await read_frame(reader)
            if kind == b"A":
                if robot.busy:
                    continue   # the last bits sent before the robot heard T
                finished = finder.feed(payload)
                if finished:
                    await stop_reply(robot)   # never more than one reply per robot
                    robot.busy = True
                    robot.reply_task = asyncio.create_task(reply(robot, finished[0]))
            elif kind == b"M":
                muted = payload[:1] == b"\x01"
                print(f"[robot] microphone {'muted' if muted else 'on'}")
                finder.reset()
            elif kind == b"D":
                await stop_reply(robot)   # D before E: the robot timed out, this reply is stale
                robot.busy = False
                finder.reset()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        robots.discard(writer)
        await stop_reply(robot)
        writer.close()
        print(f"[robot] {peer} disconnected")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pipeline
    RECORDINGS.mkdir(exist_ok=True)
    if pipeline is None:
        try:
            pipeline = build_pipeline()
        except Exception as error:   # the robot then gets T..E instead of a hang
            print(f"[chat] could not set up the voice pipeline ({type(error).__name__}: {error}): "
                  "run pip install -r firmware/requirements.txt")
    server = await asyncio.start_server(on_robot, host="0.0.0.0", port=AUDIO_PORT)
    print(f"[robot] waiting for the robot on port {AUDIO_PORT}")
    warming = asyncio.create_task(warm_up(pipeline)) if pipeline else None   # in the background
    yield
    if warming:
        warming.cancel()
        await asyncio.gather(warming, return_exceptions=True)
    for writer in list(robots):   # wait_closed() waits for open connections to end
        writer.close()
    server.close()
    await server.wait_closed()


app = FastAPI(lifespan=lifespan)
# /utterances holds what was said: only answer requests addressed to this computer
# itself, so a web page cannot read it through DNS rebinding (as webapp/salus/main.py does).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.get("/utterances")
async def list_utterances():
    return utterances


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=HTTP_PORT)

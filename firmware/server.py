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
as a WAV, and replies. Until Whisper, the model and text-to-speech are wired
in (see make_reply), the reply is the person's own words played back. That
tests the whole path: microphone -> WiFi -> laptop -> WiFi -> speaker.

Run, with the project's venv activated:
    python firmware/server.py

The robot reaches port 9000 on every network interface, so the firewall has
to let it in. The /utterances page stays on this computer (127.0.0.1:8001).
"""

import array
import asyncio
import math
import sys
import wave
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI

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

utterances: list[dict] = []          # simple in-memory log for now
robots: set[asyncio.StreamWriter] = set()


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


def to_bytes(samples: array.array) -> bytes:
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def loudness(pcm: bytes) -> float:
    samples = to_samples(pcm)
    if not samples:
        return 0.0
    mean = sum(samples) / len(samples)
    return math.sqrt(sum((s - mean) ** 2 for s in samples) / len(samples))


def louder(pcm: bytes, peak: int = 26_000, max_gain: float = 40.0) -> bytes:
    """Scale audio so its loud parts reach `peak`. The microphone records
    quietly; played back as it is, it would be hard to hear. The loudest 0.1%
    (a click or a knock) is ignored when choosing the gain, and clipped."""
    samples = to_samples(pcm)
    if not samples:
        return pcm
    mean = sum(samples) / len(samples)
    sizes = sorted(abs(s - mean) for s in samples)
    loud = sizes[int(len(sizes) * 0.999)]
    if loud == 0:
        return pcm
    gain = min(peak / loud, max_gain)
    return to_bytes(array.array("h", (max(-32768, min(32767, round((s - mean) * gain))) for s in samples)))


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


async def make_reply(wav_path: Path, pcm: bytes) -> bytes:
    """What the robot says back: 16 kHz 16-bit mono PCM, or b"" to say nothing.

    For now it plays the person's words back, louder, to test the audio path.
    The real pipeline goes here (run slow work off the event loop):

        text = await asyncio.to_thread(transcribe, wav_path)   # Whisper
        answer = await asyncio.to_thread(respond, text)        # safety check, then the local LLM
        return await asyncio.to_thread(speak, answer)          # TTS, resampled to 16 kHz mono
    """
    await asyncio.sleep(0.5)   # stands in for thinking
    return louder(pcm)


class Robot:
    """One connected robot, and whether it is waiting on us."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.busy = False      # from an utterance until the robot says D
        self.reply_task: asyncio.Task | None = None

    async def send(self, kind: bytes, payload: bytes = b"") -> None:
        self.writer.write(frame(kind, payload))
        await self.writer.drain()   # the robot reads only as fast as it plays: this paces the reply


async def reply(robot: Robot, pcm: bytes) -> None:
    seconds = len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)
    try:
        await robot.send(b"T")
        wav_path = save_wav(pcm)
        print(f"[audio] {wav_path.name}: {seconds:.1f} s")
        utterances.append({"file": str(wav_path), "seconds": round(seconds, 2)})
        speech = await make_reply(wav_path, pcm)
        speech = speech[: len(speech) // 2 * 2]   # whole samples only
        for start in range(0, len(speech), REPLY_CHUNK):
            await robot.send(b"A", speech[start : start + REPLY_CHUNK])
    except Exception as error:   # say why, and still let the robot listen again
        print(f"[reply] failed: {error!r}")
    finally:
        if not robot.writer.is_closing():
            try:
                await robot.send(b"E")
            except ConnectionError:
                pass


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
                    robot.busy = True
                    robot.reply_task = asyncio.create_task(reply(robot, finished[0]))
            elif kind == b"M":
                muted = payload[:1] == b"\x01"
                print(f"[robot] microphone {'muted' if muted else 'on'}")
                finder.reset()
            elif kind == b"D":
                robot.busy = False
                finder.reset()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        robots.discard(writer)
        if robot.reply_task:
            robot.reply_task.cancel()
        writer.close()
        print(f"[robot] {peer} disconnected")


@asynccontextmanager
async def lifespan(app: FastAPI):
    RECORDINGS.mkdir(exist_ok=True)
    server = await asyncio.start_server(on_robot, host="0.0.0.0", port=AUDIO_PORT)
    print(f"[robot] waiting for the robot on port {AUDIO_PORT}")
    yield
    for writer in list(robots):   # wait_closed() waits for open connections to end
        writer.close()
    server.close()
    await server.wait_closed()


app = FastAPI(lifespan=lifespan)


@app.get("/utterances")
async def list_utterances():
    return utterances


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=HTTP_PORT)

"""Voice chat pipeline for Salus: Whisper (speech to text) -> Gemini -> Piper (text to speech).

Audio in and out is the robot's format: 16 kHz, 16-bit little-endian, mono PCM.
Everything blocking here is meant to be called with asyncio.to_thread by the server.
The heavy libraries are imported lazily so `import chat` is cheap and testable.

Smoke test (needs firmware/.env and a downloaded voice):
    python firmware/chat.py --download-voice
    python firmware/chat.py "hello" [--wav out.wav]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

import crisis   # firmware/crisis.py, beside this file

SAMPLE_RATE = 16000
MAX_HISTORY_TURNS = 10
FALLBACK_LINE = "Sorry, I lost my train of thought, could you say that again?"

_HERE = Path(__file__).resolve().parent
_NO_SPEECH_LIMIT = 0.6
_GEMINI_TIMEOUT_MS = 15_000

# Crisis screening (crisis.py) runs on the transcript BEFORE this prompt is ever used: a
# self-harm signal gets the scripted message and Gemini is not called (CLAUDE.md invariant 2).
SYSTEM_PROMPT = """\
You are Salus, a gentle companion robot sitting on someone's desk. You are speaking \
aloud to a person who may be living with low mood or depression. Everything you write \
will be read out by a speaker, so write exactly what should be said.

Tone: warm, light, kind and a little bit happy, like a good friend who has time. \
Comforting and open, never pushy.

How to reply:
- Use 1 to 3 SHORT spoken sentences. Plain, easy words that are simple to hear.
- No markdown, no lists, no emoji, no stage directions or sounds in brackets.
- Usually end with a gentle open question or an invitation to share more, but not \
every time. Sometimes just being with them is enough.
- Be curious about small everyday things: their day, food, music, sleep, the weather, \
what made them smile, something they are looking forward to.
- Acknowledge how they feel before anything else. Never lecture and never rush to fix. \
Do not dismiss sadness or say things like "just cheer up". Do not be relentlessly cheerful.

Jokes, fun facts and quizzes (only when they ask, or say yes when you offer):
- A joke: tell ONE short, gentle, clean joke that works out loud, like a pun or a \
silly riddle, then maybe ask if they want another. Never joke about sadness, mental \
health, illness, death, bodies, weight, or any group of people.
- A fun fact: share ONE true, surprising, cheerful fact about animals, space, nature, \
food or history, in one or two sentences. Only facts you are sure are true; if unsure, \
pick a different, well-known one. Then maybe ask if they knew it.
- A quiz: a light trivia quiz about general knowledge, animals, music, food, geography \
or a topic they choose. Ask ONE question per reply with three answer options, said as \
"is it A, B, or C?", then wait. When they answer, say kindly whether it was right, give \
the right answer in a few words, and ask the next question. Five questions, then tell \
them their score warmly, whatever it is. Celebrate trying, never make a wrong answer \
feel bad, and stop at once if they want to stop or talk about something else.
- Never make a quiz, test or questionnaire about their mood, feelings, mental health \
or wellbeing, and never score or rate how they feel, even if asked. Say that is not \
something you can do, and offer a fun trivia quiz instead.

Limits:
- You are a companion, not a therapist or a doctor. Never claim to be one.
- Never diagnose, never give medical advice, and never tell anyone what medication or \
treatment to take or stop taking.
- If you are asked to do something you cannot do, say so kindly and offer to just keep \
them company or chat instead.
"""


# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class Config:
    gemini_api_key: str = field(repr=False)  # keep the key out of any printed config
    gemini_model: str = "gemini-3.5-flash-lite"
    whisper_model: str = "small.en"
    voice: str = "en_US-amy-medium"
    models_dir: Path = _HERE / "models"
    crisis_helpline: str = crisis.DEFAULT_HELPLINE   # the local crisis line sentence (Finland unless set)
    # The voice: "piper" (on this computer) or "elevenlabs" (reply text goes to ElevenLabs).
    tts: str = "piper"
    elevenlabs_api_key: str = field(default="", repr=False)
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_flash_v2_5"   # their low-latency model


def load_config() -> Config:
    """Read firmware/.env (real environment variables win). A missing key is not an error here."""
    from dotenv import load_dotenv

    load_dotenv(_HERE / ".env", override=False)
    # The web app keeps its ElevenLabs key in the project's own .env; read it too, so
    # one key serves both. firmware/.env wins where both set the same thing.
    load_dotenv(_HERE.parent / ".env", override=False)
    env = os.environ
    return Config(
        gemini_api_key=env.get("GEMINI_API_KEY", "").strip(),
        gemini_model=env.get("GEMINI_MODEL") or Config.gemini_model,
        whisper_model=env.get("SALUS_WHISPER_MODEL") or Config.whisper_model,
        voice=env.get("SALUS_VOICE") or Config.voice,
        crisis_helpline=env.get("SALUS_CRISIS_HELPLINE", "").strip() or crisis.DEFAULT_HELPLINE,
        tts=(env.get("SALUS_TTS", "").strip().lower() or Config.tts),
        elevenlabs_api_key=env.get("ELEVENLABS_API_KEY", "").strip(),
        elevenlabs_voice_id=env.get("ELEVENLABS_VOICE_ID", "").strip(),
        elevenlabs_model=env.get("ELEVENLABS_MODEL", "").strip() or Config.elevenlabs_model,
    )


# ---------------------------------------------------------------- history


def trim_history(history: list[dict], max_turns: int = MAX_HISTORY_TURNS) -> list[dict]:
    """New list with the last max_turns user+model pairs; never starts with a model entry."""
    trimmed = list(history[-max_turns * 2 :]) if max_turns > 0 else []
    while trimmed and trimmed[0]["role"] != "user":
        trimmed.pop(0)
    return trimmed


# ---------------------------------------------------------------- audio helpers


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int = SAMPLE_RATE) -> bytes:
    """Linear-interpolation resample of s16le mono PCM (numpy only)."""
    if src_rate == dst_rate:
        return pcm
    samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")
    if samples.size == 0:
        return b""
    count = max(1, round(samples.size * dst_rate / src_rate))
    positions = np.arange(count) * (src_rate / dst_rate)
    out = np.interp(positions, np.arange(samples.size), samples.astype(np.float32))
    return np.clip(np.rint(out), -32768, 32767).astype("<i2").tobytes()


# ---------------------------------------------------------------- text helpers

_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "eg", "ie"}
_TERMINATOR = re.compile(r"[.!?]+(?=\s|$)")
_MIN_SENTENCE_CHARS = 12


def split_sentences(text: str) -> list[str]:
    """Split on . ! ? and newlines, keeping punctuation; short fragments join a neighbour."""
    pieces: list[str] = []
    for line in text.splitlines():
        start = 0
        for m in _TERMINATOR.finditer(line):
            word = re.search(r"([A-Za-z.]+)$", line[start : m.start()])
            if m.group().startswith(".") and word and word.group(1).lower().strip(".") in _ABBREVIATIONS:
                continue
            pieces.append(line[start : m.end()])
            start = m.end()
        pieces.append(line[start:])
    pieces = [p.strip() for p in pieces if p.strip()]

    merged: list[str] = []
    carry = ""
    for piece in pieces:
        piece = f"{carry} {piece}".strip()
        if len(piece) < _MIN_SENTENCE_CHARS:
            carry = piece
        else:
            merged.append(piece)
            carry = ""
    if carry:
        if merged:
            merged[-1] = f"{merged[-1]} {carry}"
        else:
            merged.append(carry)
    return merged


_PUNCTUATION_MAP = str.maketrans(
    {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "..."}
)


def clean_for_speech(text: str) -> str:
    """Strip markdown, emoji and other non-ASCII symbols, and collapse whitespace."""
    text = text.translate(_PUNCTUATION_MAP)
    text = re.sub(r"(?m)^\s*(?:[-*+]|•)\s+", "", text)  # bullets
    text = re.sub(r"[*`#]+", "", text)
    text = re.sub(r"(?<!\w)_+|_+(?!\w)", "", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- speech to text


class Transcriber:
    def __init__(self, model_name: str = "small.en"):
        self._model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        from faster_whisper import WhisperModel

        threads = max(4, (os.cpu_count() or 8) // 2)
        return WhisperModel(self._model_name, device="cpu", compute_type="int8", cpu_threads=threads)

    def transcribe(self, pcm: bytes) -> str:
        """16 kHz s16le mono PCM -> text, or "" when there is no speech."""
        audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        if audio.size == 0:
            return ""
        with self._lock:
            if self._model is None:
                self._model = self._load()
            segments, _info = self._model.transcribe(
                audio,
                beam_size=1,
                language="en",
                vad_filter=True,
                condition_on_previous_text=False,
            )
            kept = [s.text for s in segments if s.no_speech_prob <= _NO_SPEECH_LIMIT]
        return " ".join(t.strip() for t in kept).strip()


# ---------------------------------------------------------------- language model


def _hit_token_limit(response) -> bool:
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
    return str(getattr(reason, "value", reason)) == "MAX_TOKENS"


class Chatter:
    def __init__(self, api_key: str, model: str = "gemini-3.5-flash-lite", client=None):
        self._api_key = api_key
        self._model = model
        self._client = client

    def _get_client(self):
        if self._client is None:
            if not self._api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not set. Copy firmware/.env.example to firmware/.env and add your key."
                )
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(timeout=_GEMINI_TIMEOUT_MS),
            )
        return self._client

    def respond(self, history: list[dict], text: str) -> str:
        """Reply to `text` given earlier turns. Does not modify `history`."""
        client = self._get_client()
        from google.genai import types

        turns = [*history, {"role": "user", "text": text}]
        contents = [types.Content(role=t["role"], parts=[types.Part(text=t["text"])]) for t in turns]
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=300,
            temperature=0.8,
        )
        response = client.models.generate_content(model=self._model, contents=contents, config=config)
        try:
            reply = (response.text or "").strip()
        except ValueError as exc:  # raised by some SDK versions when the reply is blocked
            raise RuntimeError("Gemini returned no usable reply") from exc
        if not reply:
            raise RuntimeError("Gemini returned an empty or blocked reply")
        if _hit_token_limit(response):
            # The reply was cut off mid-sentence: speak only the complete sentences.
            whole = re.match(r"(.*[.!?]+)(?=\s|$)", reply, re.DOTALL)
            if not whole:
                raise RuntimeError("Gemini reply was cut off before one full sentence")
            reply = whole.group(1).strip()
        return reply


# ---------------------------------------------------------------- text to speech


def _download_command(voice: str, models_dir: Path) -> list[str]:
    return [sys.executable, "-m", "piper.download_voices", voice, "--data-dir", str(models_dir)]


class Speaker:
    def __init__(self, voice: str = "en_US-amy-medium", models_dir: Path = _HERE / "models"):
        self._voice_name = voice
        self._models_dir = Path(models_dir)
        self._voice = None
        self._lock = threading.Lock()

    def _load(self):
        path = self._models_dir / f"{self._voice_name}.onnx"
        if not path.is_file() or not Path(f"{path}.json").is_file():
            cmd = " ".join(_download_command(self._voice_name, self._models_dir))
            raise FileNotFoundError(f"Piper voice not found at {path}. Download it with: {cmd}")
        from piper import PiperVoice

        return PiperVoice.load(path)

    def speak(self, text: str) -> bytes:
        """Text -> 16 kHz s16le mono PCM."""
        with self._lock:
            if self._voice is None:
                self._voice = self._load()
            chunks = list(self._voice.synthesize(text))
        if not chunks:
            return b""
        pcm = b"".join(c.audio_int16_bytes for c in chunks)
        return resample_pcm16(pcm, chunks[0].sample_rate, SAMPLE_RATE)


# ---------------------------------------------------------------- ElevenLabs

ELEVENLABS_API = "https://api.elevenlabs.io"
_ELEVENLABS_TIMEOUT_S = 8      # one sentence; slower than this and Piper says it instead
_ELEVENLABS_COOLDOWN_S = 60    # after a failure, Piper only for this long, so every sentence is not a wait


class ElevenLabsError(RuntimeError):
    """ElevenLabs could not speak a sentence. The message never contains the key."""


def _https(method: str, url: str, key: str, body: dict | None = None, timeout: float = _ELEVENLABS_TIMEOUT_S):
    """One request to ElevenLabs: (status, bytes). httpx brings its own certificates."""
    import httpx

    headers = {"xi-api-key": key}
    if body is not None:
        headers["Content-Type"] = "application/json"
    response = httpx.request(method, url, headers=headers, content=json.dumps(body) if body is not None else None,
                             timeout=timeout)
    return response.status_code, response.content


def _elevenlabs_problem(status: int, data: bytes, key: str) -> str:
    """A short reason from an ElevenLabs error answer, with the key taken out just in case."""
    try:
        detail = json.loads(data).get("detail")
        reason = detail.get("message") if isinstance(detail, dict) else detail
    except (ValueError, AttributeError):
        reason = None
    text = f"ElevenLabs answered {status}" + (f": {reason}" if reason else "")
    return text.replace(key, "<key>") if key else text


class ElevenLabsSpeaker:
    """Text -> 16 kHz s16le mono PCM, made by ElevenLabs. The text leaves this computer."""

    def __init__(self, api_key: str, voice_id: str, model: str = Config.elevenlabs_model, request=_https):
        self._key = api_key
        self._voice_id = voice_id
        self._model = model
        self._request = request

    def __repr__(self) -> str:
        return f"ElevenLabsSpeaker(voice_id={self._voice_id!r}, model={self._model!r})"   # never the key

    def speak(self, text: str) -> bytes:
        url = f"{ELEVENLABS_API}/v1/text-to-speech/{self._voice_id}?output_format=pcm_16000"
        status, data = self._request("POST", url, self._key, {"text": text, "model_id": self._model})
        if status != 200:
            raise ElevenLabsError(_elevenlabs_problem(status, data, self._key))
        return data[: len(data) // 2 * 2]   # raw 16 kHz 16-bit mono: what the robot plays


class SpeakerWithBackup:
    """Speak with `primary` (ElevenLabs); any sentence it cannot do is spoken by `backup` (Piper).

    After a failure the primary is left alone for `cooldown` seconds, so a dead
    connection or used-up credit costs one wait, not one per sentence."""

    def __init__(self, primary, backup, cooldown: float = _ELEVENLABS_COOLDOWN_S, clock=time.monotonic):
        self._primary = primary
        self._backup = backup
        self._cooldown = cooldown
        self._clock = clock
        self._resume_at = 0.0

    def speak(self, text: str) -> bytes:
        if self._clock() >= self._resume_at:
            try:
                pcm = self._primary(text)
                if pcm:
                    return pcm
            except Exception as error:
                print(f"[chat] {type(error).__name__}: {error}. Using Piper for the next {self._cooldown:g} s.")
                self._resume_at = self._clock() + self._cooldown
        return self._backup(text)


def make_speaker(cfg: Config) -> tuple[Callable[[str], bytes], Callable[[str], bytes], list[str]]:
    """(speak, speak_local, problems). speak_local is always Piper: the crisis message
    and the apology use it, so they never wait on the internet."""
    piper = Speaker(cfg.voice, cfg.models_dir)
    if cfg.tts != "elevenlabs":
        return piper.speak, piper.speak, []
    missing = [name for name, value in (("ELEVENLABS_API_KEY", cfg.elevenlabs_api_key),
                                        ("ELEVENLABS_VOICE_ID", cfg.elevenlabs_voice_id)) if not value]
    if missing:
        return piper.speak, piper.speak, [f"SALUS_TTS=elevenlabs but {' and '.join(missing)} is not set: using Piper"]
    eleven = ElevenLabsSpeaker(cfg.elevenlabs_api_key, cfg.elevenlabs_voice_id, cfg.elevenlabs_model)
    return SpeakerWithBackup(eleven.speak, piper.speak).speak, piper.speak, []


def find_voices(key: str, name: str, request=_https) -> list[dict]:
    """ElevenLabs voices called `name`: in the account first, then the public library."""
    from urllib.parse import urlencode

    found = []
    for where, path, query in (("your voices", "/v2/voices", {"search": name, "page_size": 20}),
                               ("voice library", "/v1/shared-voices", {"search": name, "page_size": 20})):
        status, data = request("GET", f"{ELEVENLABS_API}{path}?{urlencode(query)}", key, None, 20)
        if status != 200:
            raise ElevenLabsError(_elevenlabs_problem(status, data, key))
        for voice in json.loads(data).get("voices", []):
            labels = voice.get("labels") or {}
            found.append({
                "where": where,
                "name": voice.get("name"),
                "voice_id": voice.get("voice_id"),
                "accent": voice.get("accent") or labels.get("accent"),
                "language": voice.get("language") or labels.get("language"),
            })
    return found


# ---------------------------------------------------------------- command line


def _main() -> int:
    parser = argparse.ArgumentParser(description="Salus chat smoke test")
    parser.add_argument("text", nargs="?", help="what to say to Salus")
    parser.add_argument("--wav", metavar="PATH", help="also write the spoken reply to this WAV file")
    parser.add_argument("--download-voice", action="store_true", help="download the configured Piper voice")
    parser.add_argument("--find-voice", metavar="NAME", help="look up an ElevenLabs voice's ID by name")
    args = parser.parse_args()
    cfg = load_config()

    if args.find_voice:
        if not cfg.elevenlabs_api_key:
            parser.error("ELEVENLABS_API_KEY is not set (firmware/.env or the project's .env)")
        voices = find_voices(cfg.elevenlabs_api_key, args.find_voice)
        for v in voices:
            print(f"{v['where']:13}  {v['name']}  |  {v['voice_id']}  |  accent: {v['accent']}  |  language: {v['language']}")
        if not voices:
            print(f"No ElevenLabs voice called {args.find_voice!r} was found.")
        return 0

    if args.download_voice:
        cfg.models_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.run(_download_command(cfg.voice, cfg.models_dir)).returncode
    if not args.text:
        parser.error("give some text to say, or use --download-voice")

    if crisis.is_crisis(args.text):   # the same rule as the server: scripted, and Gemini is not asked
        reply = crisis.message(cfg.crisis_helpline)
    else:
        reply = Chatter(cfg.gemini_api_key, cfg.gemini_model).respond([], args.text)
    print(reply)
    if args.wav:
        speak, _, problems = make_speaker(cfg)
        for problem in problems:
            print(f"[chat] {problem}", file=sys.stderr)
        pcm = speak(clean_for_speech(reply))
        with wave.open(args.wav, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)
    return 0


if __name__ == "__main__":
    sys.exit(_main())

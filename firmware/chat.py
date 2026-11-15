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
import os
import re
import subprocess
import sys
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path

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


def load_config() -> Config:
    """Read firmware/.env (real environment variables win). A missing key is not an error here."""
    from dotenv import load_dotenv

    load_dotenv(_HERE / ".env", override=False)
    env = os.environ
    return Config(
        gemini_api_key=env.get("GEMINI_API_KEY", "").strip(),
        gemini_model=env.get("GEMINI_MODEL") or Config.gemini_model,
        whisper_model=env.get("SALUS_WHISPER_MODEL") or Config.whisper_model,
        voice=env.get("SALUS_VOICE") or Config.voice,
        crisis_helpline=env.get("SALUS_CRISIS_HELPLINE", "").strip() or crisis.DEFAULT_HELPLINE,
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


# ---------------------------------------------------------------- command line


def _main() -> int:
    parser = argparse.ArgumentParser(description="Salus chat smoke test")
    parser.add_argument("text", nargs="?", help="what to say to Salus")
    parser.add_argument("--wav", metavar="PATH", help="also write the spoken reply to this WAV file")
    parser.add_argument("--download-voice", action="store_true", help="download the configured Piper voice")
    args = parser.parse_args()
    cfg = load_config()

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
        pcm = Speaker(cfg.voice, cfg.models_dir).speak(clean_for_speech(reply))
        with wave.open(args.wav, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)
    return 0


if __name__ == "__main__":
    sys.exit(_main())

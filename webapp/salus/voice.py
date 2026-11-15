"""Optional ElevenLabs speech for the movement game.

The API key never reaches the browser. The page sends short text here and gets
an MP3 back; callers fall back to the browser voice when this service is not
configured or ElevenLabs is unavailable.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
DEFAULT_MODEL_ID = "eleven_flash_v2_5"
MAX_SPEECH_CHARS = 500


class VoiceError(RuntimeError):
    """A safe, key-free explanation of why speech could not be generated."""


@dataclass
class ElevenLabsVoice:
    api_key: str = field(repr=False)
    voice_id: str = DEFAULT_VOICE_ID
    model_id: str = DEFAULT_MODEL_ID
    timeout_seconds: float = 20.0

    @classmethod
    def from_environment(cls) -> "ElevenLabsVoice":
        try:
            from dotenv import load_dotenv

            project_root = Path(__file__).resolve().parents[2]
            # This project deliberately uses its root .env as the editable
            # source of truth, including when the launcher inherited stale
            # values from an earlier run.
            load_dotenv(project_root / ".env", override=True)
        except ImportError:
            pass
        return cls(
            api_key=os.environ.get("ELEVENLABS_API_KEY", "").strip(),
            voice_id=os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID).strip() or DEFAULT_VOICE_ID,
            model_id=os.environ.get("ELEVENLABS_MODEL", DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def synthesize(self, text: str) -> bytes:
        clean = " ".join(text.split())
        if not self.configured:
            raise VoiceError("ElevenLabs voice is not configured.")
        if not clean or len(clean) > MAX_SPEECH_CHARS:
            raise VoiceError("Speech text must be between 1 and 500 characters.")

        endpoint = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{quote(self.voice_id, safe='')}"
            "?output_format=mp3_44100_128"
        )
        try:
            response = httpx.post(
                endpoint,
                content=json.dumps({"text": clean, "model_id": self.model_id}),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                    "xi-api-key": self.api_key,
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise VoiceError(f"ElevenLabs rejected the speech request ({exc.response.status_code}).") from exc
        except httpx.HTTPError as exc:
            raise VoiceError("ElevenLabs could not be reached.") from exc
        audio = response.content
        if not audio:
            raise VoiceError("ElevenLabs returned no audio.")
        return audio

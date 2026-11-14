"""Short, bounded game feedback with an optional OpenAI backend."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("salus")


class ModelFeedback(BaseModel):
    """The only shape of model output the app accepts."""

    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=240)


@dataclass(frozen=True)
class Feedback:
    message: str
    source: str


def local_feedback(session: Mapping[str, Any]) -> Feedback:
    """Useful feedback when cloud feedback is disabled or unavailable."""
    found = len(session["movements_completed"])
    offered = len(session["movements_offered"])
    if session.get("feeling") == "not_for_me":
        message = "Saved. We can try a different kind of game next time."
    elif found == offered:
        message = f"Saved. You found all {found} shapes."
    elif found:
        message = f"Saved. You found {found} of {offered} shapes."
    else:
        message = "Saved. The camera game is ready whenever you want another go."
    return Feedback(message=message, source="local")


class FeedbackService:
    """Generate a brief response; always fall back locally on API failure."""

    def __init__(self, client: Any | None = None, model: str = "gpt-6-astra"):
        self.client = client
        self.model = model

    @classmethod
    def from_environment(cls) -> "FeedbackService":
        # Loading this file is optional. Environment variables still take precedence.
        try:
            from dotenv import load_dotenv

            project_root = Path(__file__).resolve().parents[2]
            load_dotenv(project_root / ".env", override=False)
        except ImportError:
            pass

        if not os.environ.get("OPENAI_API_KEY"):
            return cls()

        try:
            from openai import OpenAI

            return cls(
                client=OpenAI(timeout=8.0, max_retries=1),
                model=os.environ.get("SALUS_LLM_MODEL", "gpt-6-astra"),
            )
        except Exception:
            log.exception("OpenAI feedback could not be configured; using local feedback.")
            return cls()

    def movement_feedback(self, session: Mapping[str, Any]) -> Feedback:
        fallback = local_feedback(session)
        if self.client is None:
            return fallback

        # This is deliberately the complete payload: derived scores only, never frames,
        # video, audio, a name, free text, or a clinical interpretation.
        summary = {
            "game": session["game"],
            "mode": session["mode"],
            "duration_seconds": session["duration_seconds"],
            "movements_offered": session["movements_offered"],
            "movements_completed": session["movements_completed"],
            "movements_skipped": session["movements_skipped"],
            "tracking_quality": round(float(session["tracking_quality"]), 2),
            "feeling": session.get("feeling"),
        }
        try:
            response = self.client.responses.parse(
                model=self.model,
                store=False,
                max_output_tokens=100,
                input=[
                    {
                        "role": "developer",
                        "content": (
                            "You are the voice of Salus, a companion toy after a movement game. "
                            "Write one natural sentence, at most 30 words. Be warm but understated, "
                            "never patronising. Mention a concrete result only when useful. Do not "
                            "diagnose, call this treatment or therapy, infer health or emotion, give "
                            "medical advice, shame, pressure, or claim camera accuracy. If the person "
                            "selected not_for_me, simply accept that and suggest trying something else."
                        ),
                    },
                    {"role": "user", "content": json.dumps(summary, separators=(",", ":"))},
                ],
                text_format=ModelFeedback,
            )
            parsed = response.output_parsed
            if parsed is None:
                raise ValueError("The model returned no structured feedback.")
            message = " ".join(parsed.message.split())
            if not message or len(message) > 240:
                raise ValueError("The model feedback was outside the accepted length.")
            return Feedback(message=message, source="openai")
        except Exception as exc:
            log.warning("OpenAI feedback unavailable (%s); using local feedback.", type(exc).__name__)
            return fallback

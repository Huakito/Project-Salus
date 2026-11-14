"""Crisis screening for Salus: fixed rules, no language model.

Every transcript is checked here BEFORE anything is sent to the language model
(CLAUDE.md invariant 2). If it sounds like the person is talking about ending
their life or hurting themselves, the robot says the scripted message below and
the language model is not asked at all.

This is a list of phrases. It will miss things said another way, and it will
sometimes fire on something harmless ("I could die of embarrassment" does not
match, "I took an overdose of coffee" does). It errs towards firing. The
phrases and the message have NOT been reviewed by a clinician; they must be
before the robot is used with real people.

Nothing here prints or stores what was said.
"""

from __future__ import annotations

import re

# What the robot says. Plain ASCII, short sentences: it is read out by the speaker.
MESSAGE = (
    "Thank you for telling me. What you just said matters, and I am only a robot, "
    "so I am not the right one to help with this. "
    "Please talk to someone you trust today, or call a crisis line. "
    "If you might act on these thoughts, call your emergency number now."
)

# Each line is one way of saying it. They are matched on lower-case text with
# apostrophes removed and everything else that is not a letter turned into a space.
_PHRASES = [
    r"suicid\w*",                                   # suicide, suicidal
    r"kill(ing)? myself",
    r"end(ing)? my (own )?life",
    r"tak(e|ing) my (own )?life",
    r"end(ing)? it all",
    r"(want|wanna|wanted|going|gonna|like|ready|wish) to die",
    r"(wanna|gonna) die",
    r"wish i (was|were|wasnt|werent) (dead|alive|born)",
    r"wish i had never been born",
    r"(rather|better off) (be )?dead",
    r"better off without me",
    r"dont (want|wanna) to (live|be alive|be here|wake up|exist)",
    r"dont (wanna|want) (live|exist)",
    r"(no|any|not a) reason to (live|go on|keep going|be here)",
    r"not worth living",
    r"cant go on",
    r"(hurt|hurting|harm|harming|cut|cutting|burn|burning) myself",
    r"self harm\w*",
    r"overdos\w*",
    r"(jump|jumping) off (a|the) (bridge|building|roof)",
    r"hang(ing)? myself",
]
_SIGNAL = re.compile(r"\b(?:" + "|".join(_PHRASES) + r")\b")


def _plain(text: str) -> str:
    text = text.lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z]+", " ", text).strip()


def is_crisis(text: str) -> bool:
    """True if what was said must get the scripted message instead of the language model."""
    return _SIGNAL.search(_plain(text)) is not None


def message(helpline: str = "") -> str:
    """The scripted message, with the local helpline sentence (SALUS_CRISIS_HELPLINE) added if set."""
    helpline = helpline.strip()
    return f"{MESSAGE} {helpline}" if helpline else MESSAGE

"""The crisis rules: which sentences get the scripted message instead of the language model."""

import sys
from pathlib import Path

import pytest

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware"
sys.path.insert(0, str(FIRMWARE))

import crisis  # noqa: E402


@pytest.mark.parametrize(
    "said",
    [
        "I want to kill myself",
        "sometimes I think about killing myself.",
        "I've been feeling suicidal",
        "I keep thinking about suicide",
        "I just want to end my life",
        "I want to end it all",
        "I wanna die",
        "honestly I wish I was dead",
        "I wish I were dead",
        "everyone would be better off without me",
        "I'd be better off dead",
        "I don't want to live anymore",
        "I DON\u2019T WANT TO BE HERE anymore",
        "I don't want to wake up tomorrow",
        "there's no reason to live",
        "life is not worth living",
        "I can't go on",
        "I have been hurting myself",
        "I cut myself last night",
        "I've been self-harming",
        "I do self harm",
        "I took an overdose",
        "I thought about hanging myself",
    ],
)
def test_a_self_harm_signal_is_caught(said):
    assert crisis.is_crisis(said)


@pytest.mark.parametrize(
    "said",
    [
        "",
        "I feel a bit tired today",
        "I'm sad and I don't know why",
        "this homework is killing me",
        "my phone died",
        "I could die of embarrassment",
        "I cut my finger chopping onions",
        "I hurt my knee at football",
        "I don't want to go to school",
        "I want to live by the sea one day",
        "we watched a film about a bridge",
    ],
)
def test_ordinary_talk_is_not(said):
    assert not crisis.is_crisis(said)


def test_the_message_points_to_a_person_and_a_crisis_line():
    said = crisis.message()
    assert "someone you trust" in said and "crisis line" in said and "emergency number" in said
    assert said.isascii()   # it is read out by the speaker, and the robot's protocol is ASCII


def test_the_local_helpline_is_added_when_set():
    assert crisis.message("  Call Samaritans on 116 123.  ").endswith("emergency number now. Call Samaritans on 116 123.")
    assert crisis.message("   ") == crisis.MESSAGE


def test_the_default_helpline_is_finlands_crisis_line_and_emergency_number():
    said = crisis.message(crisis.DEFAULT_HELPLINE)
    assert said.startswith(crisis.MESSAGE)  # the scripted message is unchanged, the helpline follows it
    assert "zero nine, two five two five, zero one one one" in said  # MIELI, 09 2525 0111
    assert "call one one two" in said  # 112
    assert crisis.DEFAULT_HELPLINE.isascii()

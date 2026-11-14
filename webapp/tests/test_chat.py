import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

# chat.py needs numpy at import time; it is a firmware dependency, not a webapp one.
np = pytest.importorskip("numpy", reason="numpy (firmware/requirements.txt) is not installed")

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware"
sys.path.insert(0, str(FIRMWARE))

import chat  # noqa: E402


def pcm_of(samples) -> bytes:
    return np.asarray(samples, dtype="<i2").tobytes()


def samples_of(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2")


def sine(freq, rate, seconds=0.5, amplitude=20000):
    t = np.arange(int(rate * seconds)) / rate
    return pcm_of(np.round(amplitude * np.sin(2 * np.pi * freq * t)))


# ---------------------------------------------------------------- resample_pcm16


@pytest.mark.parametrize("src_rate", [22050, 24000, 44100, 8000])
def test_resampled_audio_has_the_length_of_the_target_rate_and_whole_samples(src_rate):
    pcm = sine(440, src_rate)
    out = chat.resample_pcm16(pcm, src_rate, 16000)
    expected = (len(pcm) // 2) * 16000 / src_rate
    assert len(out) % 2 == 0
    assert abs(len(out) // 2 - expected) <= 1


def test_odd_input_lengths_still_give_an_even_number_of_bytes():
    out = chat.resample_pcm16(pcm_of(range(1001)), 22050, 16000)
    assert len(out) % 2 == 0


def test_audio_already_at_the_target_rate_is_returned_unchanged():
    pcm = sine(440, 16000)
    assert chat.resample_pcm16(pcm, 16000, 16000) == pcm


def test_the_target_rate_defaults_to_16_khz():
    pcm = sine(440, 22050)
    assert chat.resample_pcm16(pcm, 22050) == chat.resample_pcm16(pcm, 22050, 16000)


@pytest.mark.parametrize("src_rate", [16000, 22050])
def test_empty_audio_resamples_to_empty_audio(src_rate):
    assert chat.resample_pcm16(b"", src_rate, 16000) == b""


@pytest.mark.parametrize("src_rate", [22050, 24000])
def test_a_resampled_sine_keeps_its_loudness_without_clipping_or_wrapping(src_rate):
    out = samples_of(chat.resample_pcm16(sine(1000, src_rate), src_rate, 16000))
    assert out.dtype == np.dtype("<i2")
    peak = int(np.abs(out.astype(np.int32)).max())
    assert 18000 <= peak <= 22000


def test_a_full_scale_signal_does_not_overflow_when_resampled():
    loud = pcm_of(np.round(32767 * np.sin(2 * np.pi * 1000 * np.arange(11025) / 22050)))
    out = samples_of(chat.resample_pcm16(loud, 22050, 16000)).astype(np.int32)
    # A wrapped overflow would flip a near-peak value to a large opposite value.
    assert np.abs(np.diff(out)).max() < 20000


def test_a_resampled_sine_keeps_its_pitch():
    out = samples_of(chat.resample_pcm16(sine(1000, 22050, seconds=1.0), 22050, 16000))
    spectrum = np.abs(np.fft.rfft(out.astype(np.float64)))
    freq = np.argmax(spectrum) * 16000 / len(out)
    assert abs(freq - 1000) < 5


# --------------------------------------------------------------- split_sentences


def test_several_sentences_are_split_with_their_punctuation_kept():
    text = "I hear you today. How did you sleep last night? That sounds really hard!"
    assert chat.split_sentences(text) == [
        "I hear you today.",
        "How did you sleep last night?",
        "That sounds really hard!",
    ]


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_blank_text_has_no_sentences(text):
    assert chat.split_sentences(text) == []


def test_a_decimal_number_does_not_end_a_sentence():
    text = "Version 3.5 is what I am running right now."
    assert chat.split_sentences(text) == [text]


def test_a_title_abbreviation_does_not_end_a_sentence():
    text = "I think Dr. Smith will see you on Tuesday."
    assert chat.split_sentences(text) == [text]


def test_very_short_fragments_are_merged_into_a_neighbour():
    parts = chat.split_sentences("Okay. Yes. I am really glad that you told me about it.")
    assert len(parts) < 3
    assert "".join(parts).replace(" ", "") == "Okay.Yes.Iamreallygladthatyoutoldmeaboutit."
    assert all(len(p) > len("Yes.") for p in parts)


def test_the_word_no_can_end_a_sentence():
    # Long enough that the short-fragment merge doesn't hide the split ("I said no. Then left" is merged).
    assert chat.split_sentences("I really said no. Then I left the room") == [
        "I really said no.",
        "Then I left the room",
    ]


def test_text_without_final_punctuation_comes_back_as_one_sentence():
    assert chat.split_sentences("  just keep going with me  ") == ["just keep going with me"]


def test_a_trailing_sentence_without_punctuation_is_kept():
    parts = chat.split_sentences("That is a good start for today. Let us see how tomorrow goes")
    assert parts[-1].endswith("tomorrow goes")
    assert parts[0] == "That is a good start for today."


# --------------------------------------------------------------- clean_for_speech


def test_plain_text_is_left_alone():
    assert chat.clean_for_speech("I am here with you.") == "I am here with you."


def test_bold_markers_are_removed_but_the_words_stay():
    assert chat.clean_for_speech("That is **really** brave of you") == "That is really brave of you"


def test_heading_markers_are_removed():
    cleaned = chat.clean_for_speech("# A small plan\nTake a walk")
    assert "#" not in cleaned
    assert cleaned == "A small plan Take a walk"


def test_bullet_markers_are_removed():
    cleaned = chat.clean_for_speech("- drink water\n- go outside\n* rest")
    assert cleaned == "drink water go outside rest"


def test_backticks_are_removed():
    assert chat.clean_for_speech("try `breathing` slowly") == "try breathing slowly"


def test_emoji_are_removed():
    cleaned = chat.clean_for_speech("Well done \U0001F600 you did it \U0001F389")
    assert cleaned == "Well done you did it"
    assert cleaned.isascii()


def test_runs_of_whitespace_collapse_to_single_spaces():
    assert chat.clean_for_speech("  one \n\n two\t\tthree   ") == "one two three"


# ------------------------------------------------------------------- trim_history


def make_history(turns):
    out = []
    for i in range(turns):
        out.append({"role": "user", "text": f"u{i}"})
        out.append({"role": "model", "text": f"m{i}"})
    return out


def test_a_short_history_is_unchanged():
    history = make_history(3)
    assert chat.trim_history(history) == history


def test_only_the_last_turns_are_kept():
    trimmed = chat.trim_history(make_history(15), max_turns=4)
    assert trimmed == make_history(15)[-8:]
    assert trimmed[0] == {"role": "user", "text": "u11"}
    assert trimmed[-1] == {"role": "model", "text": "m14"}


def test_the_default_limit_is_the_module_constant():
    trimmed = chat.trim_history(make_history(chat.MAX_HISTORY_TURNS + 5))
    assert len(trimmed) == chat.MAX_HISTORY_TURNS * 2


def test_a_trimmed_history_never_starts_with_a_model_reply():
    history = [{"role": "model", "text": "hello"}] + make_history(3)
    trimmed = chat.trim_history(history, max_turns=2)
    assert trimmed[0]["role"] == "user"
    # Cutting an odd-length history can land the window on a model entry.
    odd = make_history(3)[1:]  # m0 u1 m1 u2 m2
    assert chat.trim_history(odd, max_turns=10) == odd[1:]
    # The 4-entry window of u0 m0 m1 u1 m2 is m0 m1 u1 m2, which starts on a model entry.
    lopsided = [
        {"role": "user", "text": "u0"},
        {"role": "model", "text": "m0"},
        {"role": "model", "text": "m1"},
        {"role": "user", "text": "u1"},
        {"role": "model", "text": "m2"},
    ]
    assert chat.trim_history(lopsided, max_turns=2) == lopsided[3:]


def test_trimming_returns_a_new_list_and_leaves_the_input_alone():
    history = make_history(15)
    before = [dict(entry) for entry in history]
    trimmed = chat.trim_history(history, max_turns=2)
    assert trimmed is not history
    assert history == before
    short = make_history(1)
    assert chat.trim_history(short) is not short


def test_an_empty_history_trims_to_an_empty_list():
    assert chat.trim_history([]) == []


# ---------------------------------------------------------------- import hygiene


def test_importing_chat_does_not_pull_in_the_heavy_libraries():
    snippet = (
        "import sys; sys.path.insert(0, sys.argv[1]); import chat; "
        "bad = [m for m in ('faster_whisper', 'piper', 'google.genai') if m in sys.modules]; "
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", snippet, str(FIRMWARE)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, f"imported eagerly: {done.stdout.strip()} {done.stderr.strip()}"


def test_building_a_transcriber_does_not_load_a_model(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # any import now raises
    chat.Transcriber("small.en")


# ------------------------------------------------------------------- load_config

CONFIG_VARS = ["GEMINI_API_KEY", "GEMINI_MODEL", "SALUS_WHISPER_MODEL", "SALUS_VOICE"]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No config variables set, and no real firmware/.env can be read or leak into os.environ."""
    for name in CONFIG_VARS:
        # setenv first so monkeypatch restores the original state even if load_dotenv sets the name.
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    monkeypatch.setattr(chat, "_HERE", tmp_path)
    return tmp_path


def test_config_defaults_apply_and_a_missing_key_is_an_empty_string(clean_env):
    config = chat.load_config()
    assert config.gemini_api_key == ""
    assert config.gemini_model == "gemini-3.5-flash-lite"
    assert config.whisper_model == "small.en"
    assert config.voice == "en_US-amy-medium"


def test_environment_variables_override_every_default(clean_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "  test-key-123  ")
    monkeypatch.setenv("GEMINI_MODEL", "some-other-model")
    monkeypatch.setenv("SALUS_WHISPER_MODEL", "tiny.en")
    monkeypatch.setenv("SALUS_VOICE", "en_GB-alba-medium")
    config = chat.load_config()
    assert config.gemini_api_key == "test-key-123"
    assert config.gemini_model == "some-other-model"
    assert config.whisper_model == "tiny.en"
    assert config.voice == "en_GB-alba-medium"


def test_empty_variables_fall_back_to_the_defaults(clean_env, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "")
    monkeypatch.setenv("SALUS_VOICE", "")
    config = chat.load_config()
    assert config.gemini_model == "gemini-3.5-flash-lite"
    assert config.voice == "en_US-amy-medium"


def test_values_in_the_env_file_are_used(clean_env):
    (clean_env / ".env").write_text("GEMINI_API_KEY=file-key\nSALUS_VOICE=en_GB-alba-medium\n")
    config = chat.load_config()
    assert config.gemini_api_key == "file-key"
    assert config.voice == "en_GB-alba-medium"


def test_real_environment_variables_win_over_the_env_file(clean_env, monkeypatch):
    (clean_env / ".env").write_text("GEMINI_API_KEY=file-key\n")
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    assert chat.load_config().gemini_api_key == "env-key"


def test_the_config_cannot_be_changed_after_loading(clean_env):
    with pytest.raises(AttributeError):
        chat.load_config().voice = "other"


# --------------------------------------------------------------------- Transcriber


def test_transcribing_silence_of_no_samples_gives_empty_text_without_loading_a_model(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    assert chat.Transcriber("small.en").transcribe(b"") == ""
    assert chat.Transcriber("small.en").transcribe(b"\x01") == ""


# ------------------------------------------------------------------------ Speaker


def test_a_missing_voice_file_says_how_to_download_it(tmp_path):
    with pytest.raises(FileNotFoundError, match="piper.download_voices") as missing:
        chat.Speaker(voice="nonexistent", models_dir=tmp_path).speak("hi")
    assert "nonexistent" in str(missing.value)


def test_a_voice_without_its_json_sidecar_is_treated_as_missing(tmp_path):
    (tmp_path / "half.onnx").write_bytes(b"")
    with pytest.raises(FileNotFoundError, match="piper.download_voices"):
        chat.Speaker(voice="half", models_dir=tmp_path).speak("hi")


# -------------------------------------------------------------------------- Chatter

try:
    HAVE_GENAI = importlib.util.find_spec("google.genai") is not None
except ModuleNotFoundError:
    HAVE_GENAI = False

# Chatter.respond builds google.genai request objects, so it needs the real package (never the network).
needs_genai = pytest.mark.skipif(not HAVE_GENAI, reason="google-genai (firmware/requirements.txt) is not installed")


class FakeCandidate:
    def __init__(self, finish_reason):
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, text, candidates=None):
        self._text = text
        if candidates is not None:  # leave the attribute off entirely when not given
            self.candidates = candidates

    @property
    def text(self):
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


class FakeModels:
    def __init__(self, reply, candidates=None):
        self.reply = reply
        self.candidates = candidates
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return FakeResponse(self.reply, self.candidates)


class FakeClient:
    def __init__(self, reply="Hello there.", candidates=None):
        self.models = FakeModels(reply, candidates)


def spoken(contents):
    return [(c.role, c.parts[0].text) for c in contents]


@needs_genai
def test_the_reply_is_returned_without_surrounding_whitespace():
    client = FakeClient("  That sounds like a lot.\n")
    assert chat.Chatter("key", client=client).respond([], "I am tired") == "That sounds like a lot."


@needs_genai
def test_the_system_prompt_model_and_new_message_are_sent():
    client = FakeClient()
    chat.Chatter("key", model="my-model", client=client).respond([], "I am tired")
    (call,) = client.models.calls
    assert call["model"] == "my-model"
    assert call["config"].system_instruction == chat.SYSTEM_PROMPT
    assert spoken(call["contents"]) == [("user", "I am tired")]


@needs_genai
def test_earlier_turns_are_sent_before_the_new_message():
    history = [{"role": "user", "text": "hi"}, {"role": "model", "text": "hello, how are you?"}]
    client = FakeClient()
    chat.Chatter("key", client=client).respond(history, "not great")
    assert spoken(client.models.calls[0]["contents"]) == [
        ("user", "hi"),
        ("model", "hello, how are you?"),
        ("user", "not great"),
    ]


@needs_genai
def test_responding_leaves_the_history_untouched():
    history = [{"role": "user", "text": "hi"}, {"role": "model", "text": "hello"}]
    before = [dict(entry) for entry in history]
    chat.Chatter("key", client=FakeClient()).respond(history, "more")
    assert history == before


@needs_genai
@pytest.mark.parametrize("reply", [None, "", "   \n"])
def test_an_empty_reply_is_an_error(reply):
    with pytest.raises(RuntimeError, match="empty or blocked"):
        chat.Chatter("key", client=FakeClient(reply)).respond([], "hi")


@needs_genai
def test_a_blocked_reply_that_raises_inside_the_sdk_is_an_error():
    with pytest.raises(RuntimeError, match="no usable reply"):
        chat.Chatter("key", client=FakeClient(ValueError("blocked"))).respond([], "hi")


@needs_genai
def test_no_key_and_no_client_is_an_error_that_does_not_leak_the_key():
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY") as refused:
        chat.Chatter("", client=None).respond([], "hi")
    assert "AIza" not in str(refused.value)


def test_a_missing_key_is_reported_even_when_google_genai_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "google.genai", None)  # any import now raises ImportError
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        chat.Chatter("").respond([], "hi")


def reply_with(text, candidates=None):
    return chat.Chatter("key", client=FakeClient(text, candidates)).respond([], "hi")


def finished(reason):
    return [FakeCandidate(reason)]


def max_tokens_reasons():
    from google.genai import types

    return [types.FinishReason.MAX_TOKENS, "MAX_TOKENS"]


@needs_genai
@pytest.mark.parametrize("which", [0, 1], ids=["enum", "string"])
def test_a_reply_cut_off_by_the_token_limit_keeps_only_its_whole_sentences(which):
    cut = finished(max_tokens_reasons()[which])
    assert reply_with("Hi there. How are", cut) == "Hi there."
    assert reply_with("Hi there! Glad you came. And how a", cut) == "Hi there! Glad you came."


@needs_genai
@pytest.mark.parametrize("which", [0, 1], ids=["enum", "string"])
def test_a_cut_off_reply_with_no_whole_sentence_is_an_error(which):
    with pytest.raises(RuntimeError, match="cut off"):
        reply_with("How are", finished(max_tokens_reasons()[which]))


@needs_genai
def test_a_decimal_point_where_the_reply_was_cut_is_not_a_sentence_end():
    cut = finished("MAX_TOKENS")
    with pytest.raises(RuntimeError, match="cut off"):
        reply_with("I am running version 3.5", cut)
    assert reply_with("Hi there. I am running version 3.5", cut) == "Hi there."


@needs_genai
def test_a_reply_that_stopped_normally_is_returned_unchanged_even_if_it_looks_unfinished():
    from google.genai import types

    for reason in (types.FinishReason.STOP, "STOP"):
        assert reply_with("Hi there. How are", finished(reason)) == "Hi there. How are"


@needs_genai
@pytest.mark.parametrize("candidates", [None, [], [FakeCandidate(None)]], ids=["absent", "empty", "no-reason"])
def test_a_response_without_a_finish_reason_is_returned_unchanged(candidates):
    assert reply_with("Hi there. How are", candidates) == "Hi there. How are"


@needs_genai
@pytest.mark.parametrize("reply", [None, "", "  "])
def test_an_empty_reply_is_still_an_error_when_the_token_limit_was_hit(reply):
    with pytest.raises(RuntimeError, match="empty or blocked"):
        reply_with(reply, finished("MAX_TOKENS"))


@needs_genai
def test_the_output_limit_sent_to_the_model_is_300_tokens():
    client = FakeClient()
    chat.Chatter("key", client=client).respond([], "hi")
    assert client.models.calls[0]["config"].max_output_tokens == 300


# ------------------------------------------------------------- Config and files


def test_the_api_key_is_kept_out_of_the_config_repr():
    config = chat.Config("SECRETKEY")
    assert "SECRETKEY" not in repr(config)
    assert "SECRETKEY" not in str(config)
    assert config.gemini_api_key == "SECRETKEY"  # positional construction still works
    with pytest.raises(AttributeError):
        config.gemini_api_key = "other"


def test_the_speech_and_chat_libraries_have_a_minimum_version():
    lines = [line.split("#")[0].strip() for line in (FIRMWARE / "requirements.txt").read_text().splitlines()]
    for package in ("piper-tts", "google-genai", "faster-whisper"):
        (line,) = [l for l in lines if l.replace(" ", "").startswith(package)]
        assert ">=" in line, f"{package} has no lower bound: {line!r}"


def env_example_lines():
    text = (FIRMWARE / ".env.example").read_text()
    return [line.strip() for line in text.splitlines() if line.strip()]


def test_the_env_template_contains_only_assignments_and_comments():
    for line in env_example_lines():
        assert line.startswith("#") or re.fullmatch(r"[A-Z][A-Z0-9_]*=\S*", line), f"unexpected line: {line!r}"


def test_the_env_template_holds_no_secret_values():
    for line in env_example_lines():
        if line.startswith("#"):
            continue
        name, value = line.split("=", 1)
        if re.search(r"KEY|SECRET|TOKEN|PASSWORD", name):
            assert value == "", f"{name} must be empty in the template"


def test_the_env_template_documents_every_setting():
    text = (FIRMWARE / ".env.example").read_text()
    for name in ("GEMINI_API_KEY", "GEMINI_MODEL", "SALUS_WHISPER_MODEL", "SALUS_VOICE"):
        assert name in text

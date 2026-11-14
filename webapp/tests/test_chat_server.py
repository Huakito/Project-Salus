import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

# server.py imports chat.py, which needs numpy: a firmware dependency, not a webapp one.
pytest.importorskip("numpy", reason="numpy (firmware/requirements.txt) is not installed")

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware"
sys.path.insert(0, str(FIRMWARE))

import chat  # noqa: E402
import server  # noqa: E402

TWO_SENTENCES = "That sounds like a long day. Do you want to tell me more?"
FALLBACK_AUDIO = b"\x01\x00" * 3000


class FakeWriter:
    """Stands in for asyncio.StreamWriter and records every byte written."""

    def __init__(self):
        self.data = bytearray()
        self.closing = False

    def write(self, data):
        self.data += data

    async def drain(self):
        await asyncio.sleep(0)   # a real drain is a point where a task can be cancelled

    def close(self):
        self.closing = True

    def is_closing(self):
        return self.closing

    def get_extra_info(self, name, default=None):
        return ("127.0.0.1", 50000) if name == "peername" else default


class FakePipeline:
    """Fake Whisper, Gemini and Piper. Failures and delays are switched on by attributes."""

    def __init__(self, heard="I feel a bit tired today", said=TWO_SENTENCES):
        self.heard = heard
        self.said = said
        self.transcribe_error = None
        self.respond_error = None
        self.speak_error = None
        self.transcribe_delay = 0.0
        self.spoken = []
        self.histories = []
        self.respond_calls = 0

    def transcribe(self, pcm):
        time.sleep(self.transcribe_delay)
        if self.transcribe_error:
            raise self.transcribe_error
        return self.heard

    def respond(self, history, text):
        self.respond_calls += 1
        self.histories.append(history)
        if self.respond_error:
            raise self.respond_error
        return self.said

    def speak(self, text):
        self.spoken.append(text)
        if self.speak_error:
            raise self.speak_error
        return audio_for(text)


def audio_for(text):
    """Distinct, even-length fake audio per sentence, longer than one frame."""
    return text.encode("ascii") * 1000


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    """Fake pipeline in place; recordings and the log go to tmp_path or memory, never the repo."""
    fake = FakePipeline()
    monkeypatch.setattr(server, "pipeline", fake)
    monkeypatch.setattr(server, "RECORDINGS", tmp_path)
    monkeypatch.setattr(server, "utterances", [])
    monkeypatch.setattr(server, "fallback_audio", FALLBACK_AUDIO)
    return fake


async def read_frames(data):
    """Read recorded bytes back with the server's own frame reader."""
    reader = asyncio.StreamReader()
    reader.feed_data(bytes(data))
    reader.feed_eof()
    found = []
    while True:
        try:
            found.append(await server.read_frame(reader))
        except asyncio.IncompleteReadError:
            return found


def parse_frames(data):
    return asyncio.run(read_frames(data))


def parse_frames_in_loop(data):
    """Same as parse_frames, for use inside a running loop: the bytes are already complete."""
    out, view = [], bytes(data)
    while view:
        size = int.from_bytes(view[1:3], "little")
        out.append((view[:1], view[3 : 3 + size]))
        view = view[3 + size :]
    return out


def run_reply(robot, pcm=b"\x00\x01" * 8000):
    asyncio.run(server.reply(robot, pcm))
    return parse_frames(robot.writer.data)


def new_robot():
    return server.Robot(FakeWriter())


def audio_of(frames):
    return b"".join(payload for kind, payload in frames if kind == b"A")


def sentences_audio(*sentences):
    return b"".join(audio_for(s) for s in sentences)


# ---------------------------------------------------------------- the reply flow


def test_reply_sends_thinking_then_audio_then_end(fakes):
    robot = new_robot()
    frames = run_reply(robot)

    kinds = [kind for kind, _ in frames]
    assert kinds[0] == b"T" and kinds[-1] == b"E"
    assert set(kinds[1:-1]) == {b"A"} and len(kinds) > 3
    assert kinds.count(b"E") == 1
    payloads = [p for k, p in frames if k == b"A"]
    assert all(0 < len(p) <= server.REPLY_CHUNK and len(p) % 2 == 0 for p in payloads)
    assert audio_of(frames) == sentences_audio("That sounds like a long day.", "Do you want to tell me more?")


def test_reply_remembers_the_turn_and_logs_what_was_heard_and_said(fakes):
    robot = new_robot()
    run_reply(robot)

    assert robot.history == [
        {"role": "user", "text": "I feel a bit tired today"},
        {"role": "model", "text": TWO_SENTENCES},
    ]
    (entry,) = server.utterances
    assert entry["heard"] == "I feel a bit tired today"
    assert entry["said"] == TWO_SENTENCES
    assert Path(entry["file"]).parent == server.RECORDINGS and Path(entry["file"]).is_file()


def test_reply_speaks_each_sentence_separately_in_order(fakes):
    run_reply(new_robot())
    assert fakes.spoken == ["That sounds like a long day.", "Do you want to tell me more?"]


def test_reply_cleans_markdown_before_speaking(fakes):
    fakes.said = "**Hello** there, my friend. I am glad you came by today!"
    robot = new_robot()
    run_reply(robot)
    assert fakes.spoken == ["Hello there, my friend.", "I am glad you came by today!"]
    assert robot.history[-1]["text"] == "Hello there, my friend. I am glad you came by today!"


def test_reply_sends_only_whole_samples(fakes):
    fakes.speak = lambda text: b"\x01\x02\x03" * 1001   # 3003 bytes: odd
    frames = run_reply(new_robot())
    assert len(audio_of(frames)) % 2 == 0 and len(audio_of(frames)) > 0


def test_empty_transcript_sends_only_thinking_and_end(fakes):
    fakes.heard = "   "
    robot = new_robot()
    frames = run_reply(robot)

    assert frames == [(b"T", b""), (b"E", b"")]
    assert fakes.respond_calls == 0 and fakes.spoken == []
    assert robot.history == []
    assert len(server.utterances) == 1 and server.utterances[0]["heard"] == ""


def test_respond_failure_speaks_the_fallback_and_keeps_history(fakes):
    fakes.respond_error = RuntimeError("Gemini returned an empty or blocked reply")
    robot = new_robot()
    frames = run_reply(robot)

    assert frames[0] == (b"T", b"") and frames[-1] == (b"E", b"")
    assert audio_of(frames) == FALLBACK_AUDIO
    assert robot.history == []
    assert server.utterances[0]["said"] == chat.FALLBACK_LINE
    assert fakes.spoken == []   # the cached audio is used, nothing is synthesised


def test_respond_failure_without_cached_fallback_still_ends(fakes, monkeypatch):
    monkeypatch.setattr(server, "fallback_audio", None)
    fakes.respond_error = RuntimeError("no key")
    assert run_reply(new_robot()) == [(b"T", b""), (b"E", b"")]


def test_respond_failure_does_not_log_the_transcript(fakes, capsys):
    fakes.heard = "a very private sentence"
    fakes.respond_error = RuntimeError("boom")
    run_reply(new_robot())
    out = capsys.readouterr().out
    assert "RuntimeError: boom" in out and "private" not in out


def test_normal_reply_does_not_print_transcript_or_reply_text(fakes, capsys):
    run_reply(new_robot())
    out = capsys.readouterr().out
    assert "tired" not in out and "long day" not in out
    assert "[stt]" in out and "[llm]" in out and "tts first chunk" in out


def test_speak_failure_on_the_first_sentence_speaks_the_fallback_once(fakes):
    fakes.speak_error = RuntimeError("voice broke")
    robot = new_robot()
    frames = run_reply(robot)
    assert frames[0] == (b"T", b"") and frames[-1] == (b"E", b"")
    assert audio_of(frames) == FALLBACK_AUDIO   # once, not twice
    assert fakes.spoken == ["That sounds like a long day."]   # stopped at the first failure
    assert robot.history == []   # the reply was never heard
    assert server.utterances[0]["said"] == chat.FALLBACK_LINE


def test_speak_failure_without_cached_fallback_ends_in_silence(fakes, monkeypatch):
    monkeypatch.setattr(server, "fallback_audio", None)
    fakes.speak_error = RuntimeError("voice broke")
    assert run_reply(new_robot()) == [(b"T", b""), (b"E", b"")]


def test_speak_failure_after_first_sentence_keeps_what_was_sent(fakes):
    def speak(text):
        if text.startswith("Do you"):
            raise RuntimeError("voice broke")
        return audio_for(text)

    fakes.speak = speak
    robot = new_robot()
    frames = run_reply(robot)
    assert audio_of(frames) == audio_for("That sounds like a long day.")   # no apology on top
    assert frames[-1] == (b"E", b"")
    assert len(robot.history) == 2   # the robot did hear the start of it


def test_transcribe_failure_speaks_the_fallback_then_ends(fakes):
    fakes.transcribe_error = RuntimeError("no model")
    robot = new_robot()
    frames = run_reply(robot)
    assert frames[0] == (b"T", b"") and frames[-1] == (b"E", b"")
    assert audio_of(frames) == FALLBACK_AUDIO
    assert fakes.respond_calls == 0 and robot.history == []


def test_transcribe_failure_without_cached_fallback_sends_thinking_then_end(fakes, monkeypatch):
    monkeypatch.setattr(server, "fallback_audio", None)
    fakes.transcribe_error = RuntimeError("no model")
    assert run_reply(new_robot()) == [(b"T", b""), (b"E", b"")]


def test_missing_pipeline_speaks_the_fallback_then_ends(fakes, monkeypatch):
    monkeypatch.setattr(server, "pipeline", None)
    frames = run_reply(new_robot())
    assert frames[0] == (b"T", b"") and frames[-1] == (b"E", b"")
    assert audio_of(frames) == FALLBACK_AUDIO


def test_dead_connection_does_not_raise(fakes):
    robot = new_robot()

    async def broken_send(kind, payload=b""):
        raise ConnectionResetError("gone")

    robot.send = broken_send
    asyncio.run(server.reply(robot, b"\x00\x01" * 8000))   # must not raise


def test_utterance_log_is_bounded(fakes):
    robot = new_robot()

    async def many():
        for _ in range(server.MAX_UTTERANCES + 5):
            await server.reply(robot, b"\x00\x01" * 8000)

    asyncio.run(many())
    assert len(server.utterances) == server.MAX_UTTERANCES


# ---------------------------------------------------------------- memory


def test_history_is_trimmed_and_earlier_turns_reach_respond(fakes):
    robot = new_robot()

    async def talk():
        for n in range(chat.MAX_HISTORY_TURNS + 5):
            fakes.heard = f"question {n}"
            fakes.said = f"Answer number {n} is right here."
            await server.reply(robot, b"\x00\x01" * 8000)

    asyncio.run(talk())

    assert len(robot.history) == chat.MAX_HISTORY_TURNS * 2
    assert robot.history[0] == {"role": "user", "text": "question 5"}
    assert robot.history[-1]["text"] == f"Answer number {chat.MAX_HISTORY_TURNS + 4} is right here."
    # the third turn saw the first two turns
    assert [t["text"] for t in fakes.histories[2]] == [
        "question 0", "Answer number 0 is right here.", "question 1", "Answer number 1 is right here.",
    ]


def test_each_robot_has_its_own_history(fakes):
    first, second = new_robot(), new_robot()
    run_reply(first)
    assert first.history and second.history == []


# ---------------------------------------------------------------- loop and cancellation


def test_event_loop_keeps_running_while_whisper_works(fakes):
    fakes.transcribe_delay = 0.3

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        tick_task = asyncio.create_task(ticker())
        await server.reply(new_robot(), b"\x00\x01" * 8000)
        tick_task.cancel()
        await asyncio.gather(tick_task, return_exceptions=True)
        return ticks

    assert asyncio.run(scenario()) >= 15


def test_cancelling_mid_synthesis_leaves_no_task_behind(fakes):
    entered, release = threading.Event(), threading.Event()

    def slow_speak(text):
        entered.set()
        release.wait(5)
        return audio_for(text)

    fakes.speak = slow_speak
    robot = new_robot()

    async def scenario():
        task = asyncio.create_task(server.reply(robot, b"\x00\x01" * 8000))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.005)
            robot.writer.close()   # a disconnect closes the writer, then cancels the reply
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            return asyncio.all_tasks() - {asyncio.current_task()}
        finally:
            release.set()   # let the worker thread finish so the loop can shut down

    assert asyncio.run(scenario()) == set()
    assert robot.history == []   # never heard, never remembered


def test_cancelling_while_sending_audio_leaves_no_task_behind(fakes):
    fakes.said = " ".join(f"This is sentence number {n}." for n in range(6))
    robot = new_robot()

    async def drain_forever_after_first_frames():
        if len(robot.writer.data) > 3 * (3 + server.REPLY_CHUNK):
            await asyncio.Event().wait()   # the robot stopped reading
        await asyncio.sleep(0)

    robot.writer.drain = drain_forever_after_first_frames

    async def scenario():
        task = asyncio.create_task(server.reply(robot, b"\x00\x01" * 8000))
        while len(robot.writer.data) <= 3 * (3 + server.REPLY_CHUNK):
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)   # the producer is now waiting on the full queue
        robot.writer.close()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        return asyncio.all_tasks() - {asyncio.current_task()}

    assert asyncio.run(scenario()) == set()


# ---------------------------------------------------------------- warm-up


def test_warm_up_caches_the_fallback_audio(fakes, monkeypatch):
    monkeypatch.setattr(server, "fallback_audio", None)
    asyncio.run(server.warm_up(fakes))
    assert fakes.spoken == [chat.FALLBACK_LINE]
    assert server.fallback_audio == audio_for(chat.FALLBACK_LINE)


def test_warm_up_survives_a_missing_voice_and_prints_the_fix(fakes, monkeypatch, capsys):
    monkeypatch.setattr(server, "fallback_audio", None)
    fakes.speak_error = FileNotFoundError("Piper voice not found. Download it with: python -m piper.download_voices x")
    asyncio.run(server.warm_up(fakes))
    out = capsys.readouterr().out
    assert "Download it with: python -m piper.download_voices x" in out
    assert server.fallback_audio is None


def test_warm_up_survives_whisper_failing_and_says_how_to_fix_it(fakes, capsys):
    fakes.transcribe_error = ImportError("No module named 'faster_whisper'")
    asyncio.run(server.warm_up(fakes))
    out = capsys.readouterr().out
    assert "Whisper did not load" in out and "pip install -r firmware/requirements.txt" in out


def test_warm_up_prints_setup_problems(fakes, capsys):
    pipe = server.Pipeline(fakes.transcribe, fakes.respond, fakes.speak, problems=["GEMINI_API_KEY is not set: fix it"])
    asyncio.run(server.warm_up(pipe))
    assert "GEMINI_API_KEY is not set: fix it" in capsys.readouterr().out


def test_missing_gemini_key_is_reported_by_build_pipeline(monkeypatch):
    config = chat.Config(gemini_api_key="")
    monkeypatch.setattr(chat, "load_config", lambda: config)
    pipe = server.build_pipeline()
    assert any("firmware/.env" in problem for problem in pipe.problems)


# ---------------------------------------------------------------- the page


def test_utterances_endpoint_lists_heard_and_said(fakes):
    from fastapi.testclient import TestClient

    run_reply(new_robot())
    client = TestClient(server.app, base_url="http://127.0.0.1:8001")   # no `with`: the lifespan would open port 9000
    body = client.get("/utterances").json()
    assert body[0]["heard"] == "I feel a bit tired today"
    assert body[0]["said"] == TWO_SENTENCES


def test_utterances_endpoint_refuses_a_foreign_host_name(fakes):
    from fastapi.testclient import TestClient

    run_reply(new_robot())
    client = TestClient(server.app, base_url="http://127.0.0.1:8001")
    assert client.get("/utterances", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/utterances", headers={"Host": "evil.example:8001"}).status_code == 400
    assert client.get("/utterances", headers={"Host": "127.0.0.1:8001"}).status_code == 200
    assert client.get("/utterances", headers={"Host": "localhost:8001"}).status_code == 200


# ---------------------------------------------------------------- the deadline


def test_deadline_speaks_the_fallback_and_ends(fakes, monkeypatch):
    monkeypatch.setattr(server, "REPLY_DEADLINE_S", 0.1)
    release = threading.Event()

    def stuck_respond(history, text):
        release.wait(5)
        return TWO_SENTENCES

    fakes.respond = stuck_respond
    robot = new_robot()

    async def scenario():
        try:
            await asyncio.wait_for(server.reply(robot, b"\x00\x01" * 8000), timeout=2)
        finally:
            release.set()

    asyncio.run(scenario())
    frames = parse_frames(robot.writer.data)
    assert frames[0] == (b"T", b"") and frames[-1] == (b"E", b"")
    assert audio_of(frames) == FALLBACK_AUDIO
    assert robot.history == [] and server.utterances[0]["said"] == chat.FALLBACK_LINE


def test_deadline_without_cached_fallback_still_sends_end(fakes, monkeypatch):
    monkeypatch.setattr(server, "REPLY_DEADLINE_S", 0.1)
    monkeypatch.setattr(server, "fallback_audio", None)
    fakes.transcribe_delay = 0.4
    assert run_reply(new_robot()) == [(b"T", b""), (b"E", b"")]


def test_deadline_does_not_cut_off_a_reply_that_is_already_speaking(fakes, monkeypatch):
    monkeypatch.setattr(server, "REPLY_DEADLINE_S", 0.2)

    def speak(text):
        if text.startswith("Do you"):
            time.sleep(0.4)   # the second sentence is slow, but the robot is already talking
        return audio_for(text)

    fakes.speak = speak
    robot = new_robot()
    frames = run_reply(robot)
    assert audio_of(frames) == sentences_audio("That sounds like a long day.", "Do you want to tell me more?")
    assert frames[-1] == (b"E", b"") and len(robot.history) == 2


# ---------------------------------------------------------------- the robot's D, driven through on_robot


def silence(ms):
    return bytes(server.SAMPLE_RATE * server.SAMPLE_WIDTH * ms // 1000)


def utterance_pcm():
    """Quiet, then 0.6 s of loud noise, then 0.9 s of quiet: one utterance for the UtteranceFinder."""
    loud = b"\x40\x1f\xc0\xe0" * (server.SAMPLE_RATE * 600 // 1000 // 2)
    return silence(300) + loud + silence(900)


async def until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.005)


class Link:
    """The robot's end of a connection to server.on_robot."""

    def __init__(self):
        self.reader = asyncio.StreamReader()
        self.writer = FakeWriter()
        self.task = asyncio.create_task(server.on_robot(self.reader, self.writer))

    def send(self, kind, payload=b""):
        self.reader.feed_data(server.frame(kind, payload))

    def frames(self):
        return parse_frames_in_loop(self.writer.data)

    def kinds(self):
        return [kind for kind, _ in self.frames()]

    async def hang_up(self):
        self.reader.feed_eof()
        await asyncio.wait_for(self.task, timeout=2)


def test_the_finder_test_audio_is_one_utterance():
    assert len(server.UtteranceFinder().feed(utterance_pcm())) == 1


def test_d_during_a_slow_reply_cancels_it_without_a_stale_end(fakes):
    entered, release = threading.Event(), threading.Event()

    def slow_respond(history, text):
        entered.set()
        release.wait(5)
        return TWO_SENTENCES

    fakes.respond = slow_respond

    async def scenario():
        link = Link()
        try:
            link.send(b"A", utterance_pcm())
            await until(entered.is_set)
            link.send(b"D")                 # the robot's thinking timer ran out
            await asyncio.sleep(0.05)
            release.set()                   # the old reply's worker finishes late
            await asyncio.sleep(0.1)
            assert link.kinds() == [b"T"]   # nothing stale: no A, no E

            fakes.respond = lambda history, text: TWO_SENTENCES   # and the next turn is clean
            link.send(b"A", utterance_pcm())
            await until(lambda: link.kinds()[-1:] == [b"E"])
            kinds = link.kinds()
            assert kinds[0] == b"T" and kinds[1] == b"T" and kinds.count(b"E") == 1
            assert kinds[2:-1] and set(kinds[2:-1]) == {b"A"}
            await link.hang_up()
            return asyncio.all_tasks() - {asyncio.current_task()}
        finally:
            release.set()

    assert asyncio.run(scenario()) == set()


def test_a_second_utterance_after_d_gets_a_clean_reply(fakes):
    async def scenario():
        link = Link()
        link.send(b"A", utterance_pcm())
        await until(lambda: link.kinds()[-1:] == [b"E"])
        first = link.kinds()
        link.send(b"D")                     # the robot finished playing
        link.send(b"A", utterance_pcm())
        await until(lambda: link.kinds().count(b"E") == 2)
        kinds = link.kinds()
        await link.hang_up()
        return first, kinds

    first, kinds = asyncio.run(scenario())
    assert first[0] == b"T" and first[-1] == b"E" and set(first[1:-1]) == {b"A"}
    assert kinds[len(first):] == first   # the same clean T, A..., E again
    assert kinds.count(b"T") == 2


def test_disconnect_during_a_reply_cancels_it_and_leaves_no_task(fakes):
    entered, release = threading.Event(), threading.Event()

    def slow_respond(history, text):
        entered.set()
        release.wait(5)
        return TWO_SENTENCES

    fakes.respond = slow_respond

    async def scenario():
        link = Link()
        try:
            link.send(b"A", utterance_pcm())
            await until(entered.is_set)
            await link.hang_up()
            return asyncio.all_tasks() - {asyncio.current_task()}, link.kinds()
        finally:
            release.set()

    pending, kinds = asyncio.run(scenario())
    assert pending == set() and kinds == [b"T"]

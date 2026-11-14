"""The HTTP API, run against the fake board."""

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from salus.contract import DEFAULT_PATH
from salus.main import create_app
from salus.feedback import CheckInReply, Feedback, FeedbackService, local_feedback


@pytest.fixture
def contract_file(tmp_path):
    """A private copy of commands.json that a test may edit."""
    path = tmp_path / "commands.json"
    shutil.copy(DEFAULT_PATH, path)
    return path


@pytest.fixture
def client(contract_file):
    app = create_app(mode="fake", contract_path=contract_file, feedback_service=FeedbackService())
    # The app only answers to its own address, so the test client must use it.
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert app.state.manager.wait_for("connected", 2)
        yield client


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


def test_status(client):
    assert client.get("/api/status").json() == {
        "state": "connected",
        "mode": "fake",
        "port": "fake board",
        "detail": "",
        "waiting": 0,
    }


def test_the_page_is_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "<title>Salus" in page.text


def test_settings_survive_between_requests(client):
    saved = client.post("/api/settings", json={
        "spoken_guidance": False, "spoken_check_ins": True, "robot_colour": "#224466"
    })
    assert saved.status_code == 200
    assert client.get("/api/settings").json() == {
        "spoken_guidance": False, "spoken_check_ins": True, "robot_colour": "#224466"
    }


def test_commands_are_the_contents_of_commands_json(client, contract_file):
    served = client.get("/api/commands").json()
    on_disk = json.loads(contract_file.read_text())
    assert served["screen"] == on_disk["screen"]
    assert [command["key"] for command in served["commands"]] == [c["key"] for c in on_disk["commands"]]
    assert served["commands"][0]["example"] == {"text": "(^_^)", "colour": 2016}


def test_an_edit_to_commands_json_shows_up_without_a_restart(client, contract_file):
    assert client.post("/api/command", json={"key": "wave"}).status_code == 422

    contract = json.loads(contract_file.read_text())
    contract["commands"].append({"key": "wave", "label": "Wave"})
    contract_file.write_text(json.dumps(contract))

    assert "wave" in [command["key"] for command in client.get("/api/commands").json()["commands"]]
    assert client.post("/api/command", json={"key": "wave"}).json() == {"ok": True}


def test_a_broken_commands_json_is_explained(client, contract_file):
    contract_file.write_text("{ not json")
    for response in (client.get("/api/commands"), client.post("/api/command", json={"key": "spk_stop"})):
        assert response.status_code == 500
        assert "commands.json is not valid JSON" in response.json()["detail"]


# ---- sending a command ----


def test_a_command_is_sent(client):
    assert client.post("/api/command", json={"key": "spk_stop"}).json() == {"ok": True}
    assert client.post("/api/command", json={"key": "spk_set_volume", "value": 0.4}).json() == {"ok": True}


def test_a_command_missing_from_the_contract_is_not_sent(client):
    response = client.post("/api/command", json={"key": "launch_rocket"})
    assert response.status_code == 422
    assert response.json()["detail"] == '"launch_rocket" is not a command in commands.json.'


def test_a_value_of_the_wrong_kind_is_not_sent(client):
    response = client.post("/api/command", json={"key": "spk_set_volume", "value": 11})
    assert response.status_code == 422
    assert response.json()["detail"] == "spk_set_volume must be at most 1."


def test_a_value_the_firmware_would_garble_is_not_sent(client):
    response = client.post("/api/command", json={"key": "spk_play_rtttl", "value": "café"})
    assert response.status_code == 422
    assert "plain keyboard characters" in response.json()["detail"]


def test_a_refusal_from_the_board_is_passed_on(client):
    response = client.post("/api/command", json={"key": "face", "value": {"colour": 2016}})
    assert response.status_code == 502
    assert "text missing" in response.json()["detail"]


def test_a_malformed_request_gets_a_readable_answer(client):
    response = client.post("/api/command", json={"value": 1})
    assert response.status_code == 422
    assert response.json()["detail"] == "The request was not understood (key: Field required)."


# ---- the face ----


def test_a_face_is_tidied_and_its_colour_converted(client):
    response = client.post("/api/face", json={"text": "\n  (^_^)  \r\n\n", "colour": "#00ff00"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "text": "  (^_^)", "colour": 2016}


def test_a_face_of_several_lines_keeps_its_line_breaks(client):
    response = client.post("/api/face", json={"text": " /\\_/\\\n( o.o )", "colour": "#ffffff"})
    assert response.json() == {"ok": True, "text": " /\\_/\\\n( o.o )", "colour": 65535}


@pytest.mark.parametrize(
    "text, reason",
    [
        ("", "Type a face first."),
        ("(ʘ‿ʘ)", "It can't draw: ʘ ‿"),
        ("x" * 54, "Too wide"),
        ("x\n" * 31, "Too tall"),
    ],
)
def test_a_face_the_screen_cannot_show_is_not_sent(client, text, reason):
    response = client.post("/api/face", json={"text": text, "colour": "#00ff00"})
    assert response.status_code == 422
    assert reason in response.json()["detail"]


@pytest.mark.parametrize("colour", ["green", "#0f0", "00ff00", "#00ff0g", 2016])
def test_a_colour_must_be_six_hex_digits(client, colour):
    response = client.post("/api/face", json={"text": "(^_^)", "colour": colour})
    assert response.status_code == 422


# ---- connecting and disconnecting ----


def test_disconnect_then_connect(client):
    released = client.post("/api/disconnect").json()
    assert released["state"] == "released"
    assert client.get("/api/status").json() == released

    refused = client.post("/api/command", json={"key": "spk_stop"})
    assert refused.status_code == 409
    assert refused.json()["detail"].startswith("No board is connected.")

    assert client.post("/api/connect").json()["state"] == "connected"
    assert client.post("/api/command", json={"key": "spk_stop"}).json() == {"ok": True}


def test_events_start_out_empty(client):
    assert client.get("/api/events").json() == []


def test_movement_session_is_saved_without_camera_data(contract_file, data_dir):
    app = create_app(
        mode="fake",
        contract_path=contract_file,
        data_dir=data_dir,
        feedback_service=FeedbackService(),
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        session = {
            "game": "hand_signals",
            "mode": "hands",
            "duration_seconds": 95,
            "movements_offered": ["wings", "reach"],
            "movements_completed": ["wings"],
            "movements_skipped": ["reach"],
            "tracking_quality": 0.91,
            "feeling": "good",
        }
        response = client.post("/api/movement-sessions", json=session)
        assert response.status_code == 200
        assert response.json()["feedback"] == "Saved. You found 1 of 2 signals."
        assert response.json()["feedback_source"] == "local"

    saved = json.loads((data_dir / "movement-sessions.jsonl").read_text())
    assert saved["game"] == "hand_signals"
    assert saved["mode"] == "hands"
    assert "video" not in saved
    assert saved["feedback_source"] == "local"


def test_openai_feedback_receives_only_derived_session_data():
    class Parsed:
        output_parsed = type("Output", (), {"message": "Nice work — three steady shapes found."})()

    class Responses:
        def __init__(self):
            self.request = None

        def parse(self, **kwargs):
            self.request = kwargs
            return Parsed()

    client = type("Client", (), {"responses": Responses()})()
    service = FeedbackService(client=client, model="test-model")
    result = service.movement_feedback(
        {
            "game": "salus_says",
            "mode": "standing",
            "duration_seconds": 50,
            "movements_offered": ["wings", "reach", "balance"],
            "movements_completed": ["wings", "reach", "balance"],
            "movements_skipped": [],
            "tracking_quality": 0.876,
            "feeling": "good",
        }
    )
    sent = client.responses.request
    assert result == Feedback(message="Nice work — three steady shapes found.", source="openai")
    assert sent["model"] == "test-model"
    assert sent["store"] is False
    assert sent["max_output_tokens"] == 100
    assert sent["text_format"].__name__ == "ModelFeedback"
    payload = json.loads(sent["input"][1]["content"])
    assert payload["tracking_quality"] == 0.88
    assert set(payload) == {
        "game",
        "mode",
        "duration_seconds",
        "movements_offered",
        "movements_completed",
        "movements_skipped",
        "tracking_quality",
        "feeling",
    }


def test_openai_failure_falls_back_without_losing_the_session():
    class Responses:
        def parse(self, **kwargs):
            raise TimeoutError

    client = type("Client", (), {"responses": Responses()})()
    session = {
        "game": "salus_says",
        "mode": "seated",
        "duration_seconds": 20,
        "movements_offered": ["wings"],
        "movements_completed": ["wings"],
        "movements_skipped": [],
        "tracking_quality": 0.7,
        "feeling": None,
    }
    assert FeedbackService(client=client).movement_feedback(session) == local_feedback(session)


def test_movement_session_rejects_unexpected_camera_data(client):
    response = client.post(
        "/api/movement-sessions",
        json={
            "game": "salus_says",
            "mode": "standing",
            "duration_seconds": 30,
            "movements_offered": ["wings"],
            "movements_completed": [],
            "movements_skipped": [],
            "tracking_quality": 0.5,
            "video": "should not be accepted",
        },
    )
    assert response.status_code == 422
    assert "video" in response.json()["detail"]


def test_check_in_has_a_local_fallback_and_accepts_no_audio(client):
    response = client.post(
        "/api/check-in",
        json={"question": "What pace feels right?", "answer": "A little slower would be good."},
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "response": "Thanks for telling me. We can take that with us and move on when you’re ready.",
        "response_source": "local",
        "safety_pause": False,
    }

    rejected = client.post(
        "/api/check-in",
        json={"question": "How is the pace?", "answer": "Fine", "audio": "not accepted"},
    )
    assert rejected.status_code == 422
    assert "audio" in rejected.json()["detail"]


def test_check_in_uses_a_scripted_pause_for_direct_self_harm_language(client):
    response = client.post(
        "/api/check-in",
        json={"question": "What is on your mind?", "answer": "I want to hurt myself."},
    )
    assert response.status_code == 200
    assert response.json()["response_source"] == "scripted_safety"
    assert response.json()["safety_pause"] is True


def test_openai_check_in_is_bounded_and_structured():
    class Parsed:
        output_parsed = type("Output", (), {"message": "A slower pace sounds like a good fit for the next few shapes."})()

    class Responses:
        def __init__(self):
            self.request = None

        def parse(self, **kwargs):
            self.request = kwargs
            return Parsed()

    client = type("Client", (), {"responses": Responses()})()
    service = FeedbackService(client=client, model="test-model")
    reply = service.check_in_reply("What pace feels right?", "A little slower.")

    assert reply == CheckInReply(
        message="A slower pace sounds like a good fit for the next few shapes.",
        source="openai",
    )
    sent = client.responses.request
    assert sent["store"] is False
    assert sent["max_output_tokens"] == 120
    assert sent["text_format"].__name__ == "ModelCheckInReply"
    assert json.loads(sent["input"][1]["content"]) == {
        "check_in_question": "What pace feels right?",
        "answer": "A little slower.",
        "local_history": "",
    }


# ---- only this computer, and only our own page ----


def test_requests_under_another_host_name_are_refused(client):
    assert client.get("/api/status", headers={"Host": "evil.example:8000"}).status_code == 400
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/status", headers={"Host": "localhost:8000"}).status_code == 200


@pytest.mark.parametrize("origin", ["http://evil.example", "http://127.0.0.1:9999", "null"])
def test_another_website_cannot_change_anything(client, origin):
    response = client.post("/api/disconnect", headers={"Origin": origin})
    assert response.status_code == 403
    assert client.get("/api/status").json()["state"] == "connected"


def test_our_own_page_can(client):
    response = client.post("/api/disconnect", headers={"Origin": "http://127.0.0.1:8000"})
    assert response.status_code == 200


def test_responses_forbid_loading_anything_from_elsewhere(client):
    for path in ("/", "/api/status"):
        headers = client.get(path).headers
        assert headers["content-security-policy"] == (
            "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; frame-ancestors 'none'"
        )
        assert headers["permissions-policy"] == "camera=(self), microphone=(self)"
        assert headers["cache-control"] == "no-cache"


def test_the_mode_must_be_one_the_app_knows():
    with pytest.raises(ValueError, match='"real" or "fake"'):
        create_app(mode="pretend")

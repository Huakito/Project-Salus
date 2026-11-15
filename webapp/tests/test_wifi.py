"""salus/wifi.py: what the WiFi form is filled in with, and what the board reports."""

import pytest

from salus import wifi
from salus.wifi import WifiRequest, robot_wifi


def test_the_request_sends_exactly_three_things_to_the_board():
    request = WifiRequest(ssid="Home", password="12345678", laptop=" 192.168.1.20 ")
    assert request.for_board() == {"ssid": "Home", "password": "12345678", "laptop": "192.168.1.20"}


def test_the_password_is_left_out_of_the_requests_text():
    assert "12345678" not in repr(WifiRequest(ssid="Home", password="12345678", laptop="10.0.0.2"))


@pytest.mark.parametrize("hidden", ["<redacted>", "", None, "Café", "x" * 33])
def test_a_network_name_the_system_hides_or_the_board_cannot_take_is_not_offered(monkeypatch, hidden):
    monkeypatch.setattr(wifi.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(wifi, "_mac_ssid", lambda: hidden)
    assert wifi.current_ssid() is None


def test_a_network_name_the_system_tells_is_offered(monkeypatch):
    monkeypatch.setattr(wifi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(wifi, "_linux_ssid", lambda: "Home 2.4")
    assert wifi.current_ssid() == "Home 2.4"


def test_a_failing_system_command_means_no_name(monkeypatch):
    def broken():
        raise OSError("no such command")

    monkeypatch.setattr(wifi.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(wifi, "_mac_ssid", broken)
    assert wifi.current_ssid() is None


@pytest.mark.parametrize("reported", [None, 3, "connected", {"state": "dancing"}, {}])
def test_nonsense_from_the_board_is_not_shown(reported):
    assert robot_wifi(reported) is None


def test_only_the_four_known_parts_of_the_boards_report_are_kept():
    assert robot_wifi({"state": "connecting", "ssid": "Home", "ip": "", "password": "x", "laptop": 7}) == {
        "state": "connecting", "ssid": "Home", "ip": None, "laptop": None,
    }

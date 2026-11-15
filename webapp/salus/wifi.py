"""Setting up the robot's WiFi from the page, over the USB cable.

The robot reaches this computer over WiFi to stream its microphone to
firmware/server.py. It needs three things for that: the network's name, its
password, and this computer's address on that network. The page asks for them
and the server sends them to the board as the `wifi_set` command (see
commands.json); the board keeps them through power cuts.

This computer's address is found for the person. The network's name is
guessed where the system allows it (recent macOS hides it unless the app has
Location permission, so there it is usually left for the person to type).
The password is never looked up, never stored here, and never logged: it goes
straight from the request to the board.
"""

from __future__ import annotations

import ipaddress
import platform
import re
import shutil
import socket
import subprocess

from pydantic import BaseModel, ConfigDict, Field, field_validator

COMMAND_SECONDS = 3  # how long one system command may take before it is given up on

# The board's WiFi only takes 2.4 GHz WPA2-Personal or open networks. Their
# limits: a name of 1 to 32 bytes, a password of 8 to 63 characters (or none).
_PLAIN = re.compile(r"^[ -~]*$")  # plain keyboard characters: all the board can receive


class WifiRequest(BaseModel):
    """What the page sends. The password is held only long enough to pass on."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(default="", max_length=63, repr=False)
    laptop: str = Field(min_length=7, max_length=15)

    @field_validator("ssid")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        if not _PLAIN.match(value):
            raise ValueError("the network name can only use plain keyboard characters")
        return value

    @field_validator("password")
    @classmethod
    def _usable_password(cls, value: str) -> str:
        if not _PLAIN.match(value):
            raise ValueError("the password can only use plain keyboard characters")
        if 0 < len(value) < 8:
            raise ValueError("a WiFi password has at least 8 characters (leave it empty for an open network)")
        return value

    @field_validator("laptop")
    @classmethod
    def _ipv4(cls, value: str) -> str:
        try:
            address = ipaddress.IPv4Address(value.strip())
        except ValueError:
            raise ValueError("the laptop's address must look like 192.168.1.20") from None
        if address.is_unspecified or address.is_loopback or address.is_multicast:
            raise ValueError("that address cannot be reached from the robot")
        return str(address)

    def for_board(self) -> dict:
        return {"ssid": self.ssid, "password": self.password, "laptop": self.laptop}


def this_computer() -> dict:
    """This computer's address and, where the system says, its WiFi network's name."""
    return {"ip": local_ip(), "ssid": current_ssid()}


def local_ip() -> str | None:
    """The address other devices on the network reach this computer at.

    Asks the system which way it would send to an outside address. Nothing is
    actually sent: connecting a UDP socket only picks the route.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: reserved, never a real host
            ip = probe.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def current_ssid() -> str | None:
    """The WiFi network this computer is on, or None if it cannot be told."""
    system = platform.system()
    try:
        if system == "Darwin":
            name = _mac_ssid()
        elif system == "Linux":
            name = _linux_ssid()
        elif system == "Windows":
            name = _windows_ssid()
        else:
            name = None
    except (OSError, subprocess.SubprocessError):
        return None
    if not name or name.startswith("<") or not _PLAIN.match(name) or len(name) > 32:
        return None  # hidden ("<redacted>"), or a name the board could not take anyway
    return name


def _run(*command: str) -> str:
    if shutil.which(command[0]) is None:
        return ""
    done = subprocess.run(command, capture_output=True, text=True, timeout=COMMAND_SECONDS, check=False)
    return done.stdout


def _mac_ssid() -> str | None:
    ports = _run("networksetup", "-listallhardwareports")
    found = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s*\nDevice: (\S+)", ports)
    device = found.group(1) if found else "en0"
    answer = _run("networksetup", "-getairportnetwork", device)
    found = re.search(r"Current (?:Wi-Fi|AirPort) Network: (.+)", answer)
    if found:
        return found.group(1).strip()
    found = re.search(r"^\s+SSID : (.+)$", _run("ipconfig", "getsummary", device), re.MULTILINE)
    return found.group(1).strip() if found else None


def _linux_ssid() -> str | None:
    name = _run("iwgetid", "-r").strip()
    if name:
        return name
    for line in _run("nmcli", "-t", "-f", "active,ssid", "dev", "wifi").splitlines():
        if line.startswith("yes:"):
            return line[4:].replace("\\:", ":")
    return None


def _windows_ssid() -> str | None:
    found = re.search(r"^\s+SSID\s+: (.+)$", _run("netsh", "wlan", "show", "interfaces"), re.MULTILINE)
    return found.group(1).strip() if found else None


def robot_wifi(reported: object) -> dict | None:
    """The board's `wifi` event, cleaned up for /api/status, or None if it has not said."""
    if not isinstance(reported, dict):
        return None
    state = reported.get("state")
    if state not in ("connected", "connecting", "unset"):
        return None

    def text(key: str) -> str | None:
        value = reported.get(key)
        return value if isinstance(value, str) and value else None

    # Only these four: the board never sends the password, and nothing else is passed on either.
    return {"state": state, "ssid": text("ssid"), "ip": text("ip"), "laptop": text("laptop")}

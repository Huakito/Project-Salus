"""What the robot's screen can show as a face.

The screen draws text with a fixed font: every character is a 6x8 pixel cell,
multiplied by the text size. The firmware picks the biggest size that fits and
centres the art. The page applies the same rules for its live preview
(static/face.js); they are checked again here because the server has the last
word on what is sent to the board.
"""

from __future__ import annotations

CELL_WIDTH = 6  # pixels per character at text size 1
CELL_HEIGHT = 8


class FaceError(ValueError):
    """The face cannot be shown. The message says why, in words fit for the page."""


def face_lines(text: str) -> list[str]:
    """Split the art into lines, dropping what would only push it off-centre:
    spaces at the ends of lines, and blank lines above and below."""
    lines = [line.rstrip(" ") for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def check_face(text: str, screen_width: int, screen_height: int) -> list[str]:
    """Return the lines to draw, or raise FaceError if the screen cannot show them."""
    lines = face_lines(text)
    if not lines:
        raise FaceError("Type a face first.")

    # Only printable ASCII survives the trip: the firmware reads nothing else off
    # the cable, and the screen's font has no other glyphs.
    strange = list(dict.fromkeys(ch for line in lines for ch in line if not " " <= ch <= "~"))
    if strange:
        shown = " ".join("Tab" if ch == "\t" else ch for ch in strange[:8])
        raise FaceError(f"The screen can only draw plain keyboard characters. It can't draw: {shown}")

    widest = max(len(line) for line in lines)
    fits_across = screen_width // CELL_WIDTH
    if widest > fits_across:
        raise FaceError(f"Too wide: the longest line has {widest} characters and the screen fits {fits_across}.")

    fits_down = screen_height // CELL_HEIGHT
    if len(lines) > fits_down:
        raise FaceError(f"Too tall: this has {len(lines)} lines and the screen fits {fits_down}.")

    return lines


def rgb565(hex_colour: str) -> int:
    """Convert ``#rrggbb`` to the 16-bit colour the screen uses: 5 bits of red,
    6 of green, 5 of blue. ``#00ff00`` becomes 2016."""
    red, green, blue = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return (red >> 3) << 11 | (green >> 2) << 5 | blue >> 3

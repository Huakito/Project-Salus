import pytest

from salus.face import FaceError, check_face, face_lines, rgb565

SCREEN = (320, 240)  # fits 53 characters across and 30 lines down


def test_edges_that_would_push_the_art_off_centre_are_trimmed():
    assert face_lines("\n\n  (^_^)   \r\n\n") == ["  (^_^)"]


def test_blank_lines_inside_the_art_are_kept():
    assert face_lines(" o o \n\n \\_/ ") == [" o o", "", " \\_/"]


def test_every_kind_of_line_ending_is_understood():
    assert face_lines("a\r\nb\rc\nd") == ["a", "b", "c", "d"]


def test_a_face_made_of_keyboard_characters_passes():
    assert check_face(" /\\_/\\\n( o.o )\n > ^ <", *SCREEN) == [" /\\_/\\", "( o.o )", " > ^ <"]


@pytest.mark.parametrize("text", ["", "   ", "\n \n"])
def test_an_empty_face_is_refused(text):
    with pytest.raises(FaceError, match="Type a face"):
        check_face(text, *SCREEN)


def test_characters_the_screen_cannot_draw_are_named():
    with pytest.raises(FaceError) as refused:
        check_face("(ʘ‿ʘ) é\tʘ", *SCREEN)
    # Each one is named once, in the order it first appears.
    assert str(refused.value).endswith("It can't draw: ʘ ‿ é Tab")


def test_the_widest_line_must_fit_across_the_screen():
    assert check_face("x" * 53, *SCREEN)
    with pytest.raises(FaceError, match="longest line has 54 characters and the screen fits 53"):
        check_face("x" * 54, *SCREEN)


def test_the_lines_must_fit_down_the_screen():
    assert check_face("\n".join("x" * 30), *SCREEN)
    with pytest.raises(FaceError, match="has 31 lines and the screen fits 30"):
        check_face("\n".join("x" * 31), *SCREEN)


@pytest.mark.parametrize(
    "hex_colour, expected",
    [
        ("#000000", 0x0000),
        ("#ffffff", 0xFFFF),
        ("#ff0000", 0xF800),  # the screen driver's COLOR_RED
        ("#00ff00", 2016),  # COLOR_GREEN, the example in commands.json
        ("#0000ff", 0x001F),  # COLOR_BLUE
        ("#FFA500", 0xFD20),  # COLOR_ORANGE; upper-case hex works too
        ("#070307", 0),  # too dark to register: the low bits are dropped
    ],
)
def test_rgb565(hex_colour, expected):
    assert rgb565(hex_colour) == expected

"""The silent slur filter (modules/speech_filter.py): slurs go, swearing stays."""

import pytest

from modules.speech_filter import strip_slurs


def test_slurs_are_removed_silently():
    assert strip_slurs("Shut up, you retard.") == "Shut up, you."
    assert "filter" not in strip_slurs("You absolute faggot, lol.").lower()


@pytest.mark.parametrize("text", [
    "Fuck off, that was dumb as shit.",
    "I visited Niger and Pakistan.",
    "That raccoon was spicy.",
    "The fire retardant worked.",
    "",
])
def test_everything_else_is_untouched(text):
    assert strip_slurs(text) == text

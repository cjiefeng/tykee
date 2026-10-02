from __future__ import annotations

from app.decisions.text import contains_phrase, display_name_for, normalise, slugify


def test_normalise() -> None:
    assert normalise("  What to EAT, tonight?! ") == "what to eat tonight"
    assert normalise("Ｄｉｎｎｅｒ") == "dinner"  # noqa: RUF001 - NFKC full-width
    assert normalise("weekend_activity") == "weekend activity"
    assert normalise("晚餐！") == "晚餐"  # noqa: RUF001


def test_slugify() -> None:
    assert slugify("Weekend Activity!") == "weekend-activity"
    assert slugify("bubble_tea") == "bubble-tea"
    assert slugify("晚餐") == "晚餐"
    assert slugify("!!!") == ""
    assert len(slugify("x" * 100)) == 40


def test_display_name() -> None:
    assert display_name_for("weekend-activity") == "Weekend activity"


def test_contains_phrase_whole_words() -> None:
    assert contains_phrase("what s for dinner tonight", "dinner")
    assert not contains_phrase("dinnerware shopping", "dinner")
    assert contains_phrase("今晚晚餐吃什么", "晚餐")

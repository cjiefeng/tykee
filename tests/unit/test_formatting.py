from __future__ import annotations

import re

from app.telegram.formatting import TELEGRAM_LIMIT, render, to_html_chunks


def test_escapes_html() -> None:
    assert render("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    assert render("<b>not bold</b>") == "&lt;b&gt;not bold&lt;/b&gt;"


def test_markdown_subset() -> None:
    assert render("go **ramen** now") == "go <b>ramen</b> now"
    assert render("_maybe_ later") == "<i>maybe</i> later"
    assert render("snake_case_name stays") == "snake_case_name stays"
    assert (
        render("see [Maps](https://maps.example.com/?a=1&b=2)")
        == 'see <a href="https://maps.example.com/?a=1&amp;b=2">Maps</a>'
    )
    assert render("[x](javascript:alert(1))") == "[x](javascript:alert(1))"


def _balanced(chunk: str) -> bool:
    for tag in ("b", "i", "a"):
        if len(re.findall(rf"<{tag}[ >]", chunk)) != chunk.count(f"</{tag}>"):
            return False
    return True


def test_split_respects_limit_after_escaping() -> None:
    text = "\n".join(["&&& **bold** <x>" * 20] * 60)
    chunks = to_html_chunks(text)
    assert len(chunks) > 1
    assert all(len(c) <= TELEGRAM_LIMIT and _balanced(c) for c in chunks)


def test_split_single_huge_word() -> None:
    chunks = to_html_chunks("&" * 10_000)
    assert all(len(c) <= TELEGRAM_LIMIT for c in chunks)
    assert "".join(chunks) == "&amp;" * 10_000


def test_empty_text_yields_no_chunks() -> None:
    assert to_html_chunks("  \n ") == []

from __future__ import annotations

from aiogram.types import MessageEntity, Sticker

from app.telegram.addressing import classify, is_addressed, parse_command, stored_text
from tests.conftest import BOT, mention, tg_message, tg_user


def test_mention_addresses_bot() -> None:
    text, ents = mention("dinner?")
    assert is_addressed(tg_message(text, entities=ents), BOT)


def test_mention_is_case_insensitive_and_utf16_safe() -> None:
    text = "😀 @tykeebot hi"  # emoji is 2 UTF-16 units
    ents = [MessageEntity(type="mention", offset=3, length=9)]
    assert is_addressed(tg_message(text, entities=ents), BOT)


def test_other_mention_does_not_address() -> None:
    text, ents = mention("dinner?", handle="@SomeoneElse")
    assert not is_addressed(tg_message(text, entities=ents), BOT)


def test_text_mention_by_id() -> None:
    ents = [MessageEntity(type="text_mention", offset=0, length=5, user=tg_user(BOT.id))]
    assert is_addressed(tg_message("Tykee pick one", entities=ents), BOT)


def test_reply_to_bot_addresses() -> None:
    bot_msg = tg_message("earlier reply", from_id=BOT.id)
    assert is_addressed(tg_message("and tomorrow?", reply_to=bot_msg), BOT)
    human = tg_message("earlier", from_id=222)
    assert not is_addressed(tg_message("lol", reply_to=human), BOT)


def test_plain_chatter_is_not_addressed() -> None:
    assert not is_addressed(tg_message("reaching in 5"), BOT)


def test_parse_command() -> None:
    assert parse_command("/help", "TykeeBot") is not None
    cmd = parse_command("/Pick@tykeebot dinner tonight", "TykeeBot")
    assert cmd is not None and (cmd.name, cmd.args) == ("pick", "dinner tonight")
    assert parse_command("/pick@OtherBot dinner", "TykeeBot") is None
    assert parse_command("not /a command", "TykeeBot") is None
    assert parse_command("/", "TykeeBot") is None


def test_classify_and_placeholders() -> None:
    assert classify(tg_message("hi")) == "text"
    assert classify(tg_message("😂👍🏽")) == "emoji"
    assert classify(tg_message("ok 👍")) == "text"
    sticker = Sticker(
        file_id="f", file_unique_id="u", type="regular", width=1, height=1,
        is_animated=False, is_video=False, emoji="🍜",
    )  # fmt: skip
    msg = tg_message(None, sticker=sticker)
    assert classify(msg) == "sticker"
    assert stored_text(msg, "sticker") == "[sticker 🍜]"

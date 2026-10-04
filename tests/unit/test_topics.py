"""Forum topics (§10.4): read every topic, answer only in the answer topic."""

from __future__ import annotations

import json
from typing import Any

from aiogram.types import CallbackQuery, ForumTopicClosed, ForumTopicCreated, ForumTopicEdited

from app.llm.client import LLMUnavailable
from app.settings import set_value
from app.telegram.keyboards import callback_data
from app.telegram.topics import GENERAL_THREAD, KEY_ANSWER, send_thread, thread_of
from tests.conftest import (
    BOT,
    GROUP_ID,
    JACK_TG,
    PARTNER_TG,
    Env,
    Stack,
    make_stack,
    mention,
    seed_category,
    tg_message,
    tg_user,
)
from tests.fakes.fake_llm import FakeLLMClient

ANSWER = 5
OTHER = 9


async def _answer_topic(env: Env, thread: int | None = ANSWER) -> None:
    await env.db.write(lambda c: set_value(c, KEY_ANSWER, thread))


async def _rows(env: Env) -> list[dict[str, Any]]:
    rows = await env.db.read(
        lambda c: c.execute("SELECT role, thread_id, content FROM messages ORDER BY id").fetchall()
    )
    return [dict(r) for r in rows]


async def _say(
    stack: Stack,
    env: Env,
    text: str,
    topic: int | None,
    *,
    at_bot: bool = False,
    from_id: int = JACK_TG,
    **kw: Any,
) -> None:
    actor = env.jack if from_id == JACK_TG else env.partner
    if at_bot:
        body, ents = mention(text)
        msg = tg_message(body, entities=ents, from_id=from_id, topic=topic, forum=True, **kw)
    else:
        msg = tg_message(text, from_id=from_id, topic=topic, forum=True, **kw)
    await stack.adapter.handle_message(msg, actor)


# --- thread ids --------------------------------------------------------------------------------


def test_thread_of() -> None:
    assert thread_of(tg_message("x")) is None  # group without topics
    assert thread_of(tg_message("x", forum=True)) == GENERAL_THREAD
    assert thread_of(tg_message("x", topic=OTHER)) == OTHER
    assert thread_of(tg_message("x", chat_id=JACK_TG, chat_type="private")) is None
    # a reply in General carries message_thread_id but isn't a topic message
    assert thread_of(tg_message("x", forum=True, message_thread_id=123)) == GENERAL_THREAD


def test_send_thread_omits_general() -> None:
    assert (send_thread(None), send_thread(GENERAL_THREAD), send_thread(7)) == (None, None, 7)


# --- topic names -------------------------------------------------------------------------------


async def test_topic_names_from_service_messages(env: Env) -> None:
    stack = make_stack(env)
    food = ForumTopicCreated(name="Food", icon_color=1)
    created = tg_message(None, topic=OTHER, forum_topic_created=food)
    await stack.adapter.handle_message(created, env.jack)
    await stack.adapter.handle_message(
        tg_message(None, topic=OTHER, forum_topic_edited=ForumTopicEdited(name="Food & drinks")),
        env.jack,
    )
    closed = tg_message(None, topic=OTHER, forum_topic_closed=ForumTopicClosed())
    await stack.adapter.handle_message(closed, env.jack)
    await _say(stack, env, "hello", None)  # General
    topics = {t.thread_id: t for t in await stack.topics.known(GROUP_ID)}
    assert topics[OTHER].name == "Food & drinks" and topics[OTHER].closed
    assert topics[GENERAL_THREAD].name == "General" and topics[GENERAL_THREAD].messages == 1
    assert [r["thread_id"] for r in await _rows(env)] == [GENERAL_THREAD]  # service msgs not stored


async def test_name_learned_from_reply_to_creation_message(env: Env) -> None:
    stack = make_stack(env)
    movies = ForumTopicCreated(name="Movies", icon_color=1)
    creation = tg_message(None, topic=12, forum_topic_created=movies)
    await _say(stack, env, "arrival tonight?", 12, reply_to=creation)
    assert await stack.topics.name_of(GROUP_ID, 12) == "Movies"
    assert await stack.topics.name_of(GROUP_ID, 77) == "Topic 77"


# --- gate --------------------------------------------------------------------------------------


async def test_no_answer_topic_means_speak_anywhere(env: Env) -> None:
    stack = make_stack(env, FakeLLMClient("Hi"))
    await _say(stack, env, "hi", OTHER, at_bot=True)
    assert stack.gateway.sent[-1].text == "Hi" and stack.gateway.sent[-1].thread_id == OTHER


async def test_ignored_topics_are_never_stored(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, "telegram.ignored_topic_ids", [OTHER]))
    stack = make_stack(env)
    await _say(stack, env, "private stuff", OTHER, at_bot=True)
    assert await _rows(env) == [] and stack.gateway.sent == [] and stack.llm.requests == []


async def test_off_topic_is_stored_but_never_answered(env: Env) -> None:
    await _answer_topic(env)
    stack = make_stack(env)
    await _say(stack, env, "laksa later?", OTHER)
    await _say(stack, env, "what should we eat", OTHER, at_bot=True)
    await stack.adapter.handle_message(tg_message("/pick dinner", topic=OTHER), env.jack)
    assert stack.gateway.sent == [] and stack.llm.requests == []
    assert [r["thread_id"] for r in await _rows(env)] == [OTHER] * 3
    assert not stack.ambient._debouncer.pending(GROUP_ID)  # no judge outside the answer topic


async def test_off_topic_redirect_once_per_day(env: Env) -> None:
    await _answer_topic(env)
    await env.db.write(lambda c: set_value(c, "telegram.off_topic_mention", "redirect"))
    stack = make_stack(env)
    await stack.topics.seen(GROUP_ID, ANSWER, name="Tykee")
    await _say(stack, env, "dinner?", OTHER, at_bot=True)
    await _say(stack, env, "dinner??", OTHER, at_bot=True)
    sent = [(s.text, s.thread_id) for s in stack.gateway.sent]
    assert sent == [("Ask me in **Tykee** 👋", OTHER)]
    stack.clock.advance(days=1)
    await _say(stack, env, "dinner???", OTHER, at_bot=True)
    assert len(stack.gateway.sent) == 2


async def test_answer_topic_reply_and_history_are_topic_only(env: Env) -> None:
    await _answer_topic(env)
    llm = FakeLLMClient("Hi.", "Pho.")
    stack = make_stack(env, llm)
    await _say(stack, env, "secret elsewhere", OTHER)
    await _say(stack, env, "earlier here", ANSWER, from_id=PARTNER_TG)
    await _say(stack, env, "dinner?", ANSWER, at_bot=True)
    sent = stack.gateway.sent[-1]
    assert (sent.text, sent.thread_id) == ("Pho.", ANSWER)
    history = json.dumps(list(llm.requests[-1].messages))
    assert "earlier here" in history and "secret elsewhere" not in history
    last = (await _rows(env))[-1]
    assert (last["role"], last["thread_id"]) == ("assistant", ANSWER)


async def test_general_as_answer_topic_sends_without_thread_id(env: Env) -> None:
    await _answer_topic(env, GENERAL_THREAD)
    stack = make_stack(env, FakeLLMClient("ok"))
    await _say(stack, env, "hi", None, at_bot=True)
    assert stack.gateway.sent[-1].thread_id is None
    assert (await _rows(env))[-1]["thread_id"] == GENERAL_THREAD


async def test_ambient_only_listens_and_speaks_in_answer_topic(env: Env) -> None:
    await _answer_topic(env)
    await env.db.write(lambda c: set_value(c, "telegram.answer_topic_mode", "ambient"))
    verdict = json.dumps({"action": "respond", "reason": "stuck", "confidence": 0.9})
    llm = FakeLLMClient(verdict, "Laksa!")
    stack = make_stack(env, llm)
    await _say(stack, env, "idk you decide", OTHER, from_id=PARTNER_TG)
    assert not stack.ambient._debouncer.pending(GROUP_ID)
    await _say(stack, env, "idk you decide", ANSWER, from_id=PARTNER_TG)
    await stack.ambient.fire(GROUP_ID)
    judged = str(next(iter(llm.requests[0].messages))["content"])
    assert judged.count("idk you decide") == 1  # the off-topic copy isn't in the judge window
    assert (stack.gateway.sent[-1].text, stack.gateway.sent[-1].thread_id) == ("Laksa!", ANSWER)


# --- Tykee's own topic (answer_topic_mode = addressed) -------------------------------------------


async def test_own_topic_answers_without_mention_unquoted(env: Env) -> None:
    await _answer_topic(env)
    stack = make_stack(env, FakeLLMClient("Chicken rice."))
    await _say(stack, env, "what should we eat", ANSWER)
    sent = stack.gateway.sent[-1]
    assert (sent.text, sent.thread_id, sent.reply_to) == ("Chicken rice.", ANSWER, None)
    assert not stack.ambient._debouncer.pending(GROUP_ID)


async def test_own_topic_mention_is_still_quoted(env: Env) -> None:
    await _answer_topic(env)
    stack = make_stack(env, FakeLLMClient("Ramen."))
    await _say(stack, env, "dinner?", ANSWER, at_bot=True)
    assert stack.gateway.sent[-1].reply_to is not None


async def test_own_topic_skips_stickers_and_emoji(env: Env) -> None:
    await _answer_topic(env)
    stack = make_stack(env)
    await _say(stack, env, "😂👍", ANSWER)
    sticker = tg_message(
        None,
        topic=ANSWER,
        sticker={
            "file_id": "s",
            "file_unique_id": "s",
            "type": "regular",
            "width": 1,
            "height": 1,
            "is_animated": False,
            "is_video": False,
        },
    )
    await stack.adapter.handle_message(sticker, env.jack)
    assert stack.gateway.sent == [] and stack.llm.requests == []


async def test_own_topic_respects_mute_but_mentions_get_through(env: Env) -> None:
    await _answer_topic(env)
    stack = make_stack(env, FakeLLMClient("Sure."))
    await stack.ambient.mute(GROUP_ID)
    await _say(stack, env, "what should we eat", ANSWER)
    assert stack.gateway.sent == [] and stack.llm.requests == []
    await _say(stack, env, "ok what should we eat", ANSWER, at_bot=True)
    assert stack.gateway.sent[-1].text == "Sure."


async def test_own_topic_offline_fallback_only_for_explicit_asks(env: Env) -> None:
    await _answer_topic(env)
    stack = make_stack(env, FakeLLMClient(LLMUnavailable("down"), LLMUnavailable("down")))
    await _say(stack, env, "see you later", ANSWER)
    assert stack.gateway.sent == []
    await _say(stack, env, "you there?", ANSWER, at_bot=True)
    assert len(stack.gateway.sent) == 1


async def test_general_answer_topic_and_ambient_mode_need_a_mention(env: Env) -> None:
    await _answer_topic(env, GENERAL_THREAD)
    stack = make_stack(env)
    await _say(stack, env, "what should we eat", None)
    assert stack.gateway.sent == [] and stack.ambient._debouncer.pending(GROUP_ID)
    await _answer_topic(env)
    await env.db.write(lambda c: set_value(c, "telegram.answer_topic_mode", "ambient"))
    await _say(stack, env, "what should we eat", ANSWER)
    assert stack.gateway.sent == [] and stack.llm.requests == []


# --- /settopic ---------------------------------------------------------------------------------


async def test_settopic_admin_only(env: Env) -> None:
    await _answer_topic(env)
    stack = make_stack(env)
    partner_cmd = tg_message("/settopic", from_id=PARTNER_TG, topic=OTHER)
    await stack.adapter.handle_message(partner_cmd, env.partner)
    assert await stack.topics.answer_topic() == ANSWER and stack.gateway.sent == []
    await stack.adapter.handle_message(tg_message("/settopic", topic=OTHER), env.jack)
    assert await stack.topics.answer_topic() == OTHER
    last = stack.gateway.sent[-1]
    assert (last.text, last.thread_id) == ("I'll hang out here now 👋", OTHER)
    await stack.adapter.handle_message(tg_message("/settopic"), env.jack)  # group without topics
    assert "doesn't use topics" in stack.gateway.sent[-1].text


async def test_env_seeds_answer_topic_only_once(env: Env) -> None:
    from app.telegram.topics import seed_answer_topic, stored_answer_topic

    await env.db.write(lambda c: seed_answer_topic(c, 42))
    await env.db.write(lambda c: set_value(c, KEY_ANSWER, None))  # admin cleared it later
    await env.db.write(lambda c: seed_answer_topic(c, 42))  # restart: env must not re-apply
    assert await env.db.read(stored_answer_topic) is None


# --- sending -----------------------------------------------------------------------------------


async def test_callback_replies_go_to_answer_topic(env: Env) -> None:
    await _answer_topic(env)
    await seed_category(env, "dinner", [("Pho", [])])
    stack = make_stack(env)
    await stack.adapter.handle_message(tg_message("/pick dinner", topic=ANSWER), env.jack)
    pick = stack.gateway.sent[-1]
    assert pick.thread_id == ANSWER
    did = (await env.db.read(lambda c: c.execute("SELECT id FROM decisions").fetchone()))[0]
    msg = tg_message("bot", from_id=BOT.id, message_id=pick.message_id, topic=ANSWER)
    cb = CallbackQuery(
        id="c",
        from_user=tg_user(JACK_TG),
        chat_instance="i",
        data=callback_data(did, "accept"),
        message=msg,
    )
    await stack.adapter.handle_callback(cb, env.jack)
    last = stack.gateway.sent[-1]
    assert (last.text, last.thread_id) == ("✅ **Pho** it is.", ANSWER)


async def test_deleted_answer_topic_raises_health_and_dms_admin_once(env: Env) -> None:
    await _answer_topic(env)
    stack = make_stack(env, FakeLLMClient("one", "two"))
    stack.gateway.fail_threads = {ANSWER}
    await _say(stack, env, "hi", ANSWER, at_bot=True)
    await _say(stack, env, "hi again", ANSWER, at_bot=True)
    error = stack.health.answer_topic_error
    assert error is not None and "thread 5" in error
    dms = [s for s in stack.gateway.sent if s.chat_id == JACK_TG]
    assert len(dms) == 1 and "/settopic" in dms[0].text
    # fixing it with /settopic clears the error
    stack.gateway.fail_threads = set()
    await stack.adapter.handle_message(tg_message("/settopic", topic=OTHER), env.jack)
    assert stack.health.answer_topic_error is None


async def test_closed_answer_topic_is_handled_like_a_deleted_one(env: Env) -> None:
    await _answer_topic(env)
    stack = make_stack(env, FakeLLMClient("one"))
    stack.gateway.fail_threads = {ANSWER}
    stack.gateway.fail_error = "Bad Request: TOPIC_CLOSED"
    await _say(stack, env, "hi", ANSWER, at_bot=True)
    assert stack.health.answer_topic_error is not None
    assert [s.chat_id for s in stack.gateway.sent] == [JACK_TG]  # the admin DM


async def test_callback_after_topic_moved_goes_to_new_topic_unquoted(env: Env) -> None:
    await _answer_topic(env)
    await seed_category(env, "dinner", [("Pho", [])])
    stack = make_stack(env)
    await stack.adapter.handle_message(tg_message("/pick dinner", topic=ANSWER), env.jack)
    pick = stack.gateway.sent[-1]
    did = (await env.db.read(lambda c: c.execute("SELECT id FROM decisions").fetchone()))[0]
    await stack.topics.set_answer_topic(OTHER)  # moved while the buttons were still open
    msg = tg_message("bot", from_id=BOT.id, message_id=pick.message_id, topic=ANSWER)
    cb = CallbackQuery(
        id="c",
        from_user=tg_user(JACK_TG),
        chat_instance="i",
        data=callback_data(did, "accept"),
        message=msg,
    )
    await stack.adapter.handle_callback(cb, env.jack)
    last = stack.gateway.sent[-1]
    assert (last.thread_id, last.reply_to) == (OTHER, None)


async def test_moving_answer_topic_does_not_harvest_old_one(env: Env) -> None:
    await _answer_topic(env)
    stack = make_stack(env, FakeLLMClient(*["ok"] * 6))
    for t in ["a", "b", "c", "d", "e", "f"]:
        await _say(stack, env, t, ANSWER)
    await stack.topics.set_answer_topic(OTHER)
    newest = (await env.db.read(lambda c: c.execute("SELECT MAX(id) FROM messages").fetchone()))[0]
    cursor = await env.db.read(
        lambda c: c.execute(
            "SELECT last_msg_id FROM topic_harvest WHERE chat_id = ? AND thread_id = ?",
            (GROUP_ID, ANSWER),
        ).fetchone()
    )
    assert cursor is not None and cursor[0] == newest


async def test_ignored_topic_cant_be_answer_topic(env: Env) -> None:
    import pytest

    await env.db.write(lambda c: set_value(c, "telegram.ignored_topic_ids", [OTHER]))
    stack = make_stack(env)
    with pytest.raises(ValueError, match="ignored"):
        await stack.topics.set_answer_topic(OTHER)

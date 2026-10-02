"""Admin dashboard (§11): security, every page, and the actions that change behaviour."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest

from app import inbox_appliers
from app.dashboard.app import create_app
from app.dashboard.core import DashboardDeps, hash_password
from app.harvest import Harvester
from app.importer.service import ImportService
from app.settings import get_value, set_value
from app.telegram.topics import KEY_ANSWER
from tests.conftest import GROUP_ID, TZ, Env, Stack, make_stack, seed_category, tg_message
from tests.fakes.fake_batches import FakeBatches
from tests.fakes.fake_llm import FakeLLMClient
from tests.fakes.telegram_export import dinner_chat, dumps, single_chat
from tests.unit.test_import_flow import CATEGORIES, NOTES, extraction

PASSWORD = "correct horse battery staple"
_HASH = hash_password(PASSWORD)  # argon2 is slow by design; hash once per module
LAN = ("192.168.1.20", 50000)


@dataclass
class Dash:
    client: httpx.AsyncClient
    stack: Stack
    deps: DashboardDeps
    csrf: str = ""

    async def login(self) -> None:
        await self.client.get("/login")
        r = await self.client.post(
            "/login", data={"password": PASSWORD, "csrf": await self.token("/login")}
        )
        assert r.status_code == 303 and r.headers["location"] == "/"
        self.csrf = await self.token("/")

    async def token(self, page: str) -> str:
        html = (await self.client.get(page)).text
        m = re.search(r'name="csrf" value="([^"]+)"', html)
        assert m, f"no csrf field on {page}"
        return m.group(1)

    async def post(
        self, url: str, data: dict[str, str] | None = None, **kw: object
    ) -> httpx.Response:
        return await self.client.post(url, data={"csrf": self.csrf, **(data or {})}, **kw)  # type: ignore[arg-type]

    async def flash(self, page: str) -> str:
        return (await self.client.get(page)).text


def _client(app: object, ip: tuple[str, int] = LAN) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=ip),  # type: ignore[arg-type]
        base_url="http://nas.local",
    )


@pytest.fixture
async def dash(env: Env) -> AsyncIterator[Dash]:
    stack = make_stack(env)
    inbox_appliers.register(stack.memory, stack.decisions)
    harvester = Harvester(
        db=env.db,
        settings=env.settings,
        llm=stack.llm,
        memory=stack.memory,
        decisions=stack.decisions,
        topics=stack.topics,
        users=env.users,
        tz=TZ,
        group_id=lambda: GROUP_ID,
        health=stack.health,
        clock=stack.clock,
    )
    deps = DashboardDeps(
        db=env.db,
        settings=env.settings,
        store=stack.store,
        memory=stack.memory,
        decisions=stack.decisions,
        topics=stack.topics,
        health=stack.health,
        gateway=stack.gateway,
        users=env.users,
        tz=TZ,
        group_id=lambda: GROUP_ID,
        db_path=env.db.path,
        password_hash=_HASH,
        session_secret="s" * 40,
        log_lines=lambda: ['{"msg": "hello log"}'],
        harvester=harvester,
        ambient=stack.ambient,
        embed_model="fake@test",
        importer=ImportService(
            db=env.db,
            settings=env.settings,
            llm=FakeLLMClient(CATEGORIES, NOTES),
            batches=FakeBatches(responder=extraction),
            store=stack.store,
            users=env.users,
            tz=TZ,
            imports_dir=env.vault.parent / "imports",
            clock=stack.clock,
        ),
    )
    async with _client(create_app(deps)) as client:
        yield Dash(client, stack, deps)


# --- security --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ip", "ok"),
    [
        ("8.8.8.8", False),
        ("192.168.1.5", True),
        ("10.0.0.2", True),
        ("100.101.102.103", True),
        ("172.32.0.1", False),
    ],
)
async def test_lan_only(dash: Dash, ip: str, ok: bool) -> None:
    async with _client(dash.client._transport.app, (ip, 1)) as c:  # type: ignore[attr-defined]
        r = await c.get("/healthz")
    assert (r.status_code == 200) is ok


async def test_pages_require_login_but_healthz_doesnt(dash: Dash) -> None:
    r = await dash.client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    h = await dash.client.get("/healthz")
    assert h.status_code == 200 and h.json()["ok"] is True


async def test_login_wrong_password_and_lockout(dash: Dash) -> None:
    token = await dash.token("/login")
    for _ in range(5):
        r = await dash.client.post("/login", data={"password": "nope", "csrf": token})
    assert r.status_code == 401
    r = await dash.client.post("/login", data={"password": PASSWORD, "csrf": token})
    assert r.status_code == 429 and "Too many attempts" in r.text


async def test_login_sets_strict_httponly_cookie(dash: Dash) -> None:
    await dash.client.get("/login")
    r = await dash.client.post(
        "/login", data={"password": PASSWORD, "csrf": await dash.token("/login")}
    )
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert (await dash.client.get("/")).status_code == 200


async def test_login_requires_csrf(dash: Dash) -> None:
    await dash.client.get("/login")
    r = await dash.client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 403


async def test_posts_require_csrf(dash: Dash) -> None:
    await dash.login()
    r = await dash.client.post("/settings", data={"key": "history.max_turns", "value": "3"})
    assert r.status_code == 403
    r = await dash.client.post(
        "/settings",
        data={"key": "history.max_turns", "value": "3"},
        headers={"X-CSRF-Token": dash.csrf},
    )
    assert r.status_code == 303


async def test_logout(dash: Dash) -> None:
    await dash.login()
    await dash.post("/logout")
    assert (await dash.client.get("/")).status_code == 303


# --- pages render ----------------------------------------------------------------------------


async def test_every_page_renders(dash: Dash, env: Env) -> None:
    cid = await seed_category(env, "dinner", [("Pho", ["soup"])])
    await dash.stack.store.ensure_skeleton(env.users)
    await dash.stack.adapter.handle_message(tg_message("hello", topic=9), env.jack)
    await dash.stack.memory.propose(
        owner="jack", content="Likes teh.", reason="r", topic="drinks", source="s"
    )
    await dash.login()
    pages = [
        "/",
        "/behaviour",
        "/ambient",
        "/settings",
        "/categories",
        f"/categories/{cid}",
        "/memory",
        "/memory?q=jack",
        "/memory/note?path=people/jack.md",
        "/memory/inbox",
        "/memory/inbox?status=approved",
        "/conversations",
        f"/conversations/{GROUP_ID}",
        "/users",
        "/system",
        "/system/logs",
        "/import",
    ]
    for page in pages:
        r = await dash.client.get(page)
        assert r.status_code == 200, (page, r.status_code, r.text[:300])
    overview = (await dash.client.get("/")).text
    assert "Spend today" in overview and "not set: answering in every topic" in overview
    assert "hello log" in (await dash.client.get("/system")).text


# --- behaviour & settings --------------------------------------------------------------------


async def test_settings_are_validated_before_saving(dash: Dash, env: Env) -> None:
    await dash.login()
    await dash.post("/behaviour/settings", {"history.max_turns": "abc"})
    assert "isn&#39;t a number" in await dash.flash("/behaviour")
    await dash.post("/settings", {"key": "ambient.threshold", "value": '"high"'})
    assert "Not saved" in await dash.flash("/settings")
    await dash.post("/settings", {"key": "telegram.off_topic_mention", "value": '"shout"'})
    assert "Not saved" in await dash.flash("/settings")
    await dash.post("/settings", {"key": "x", "value": "{not json"})
    assert "not valid JSON" in await dash.flash("/settings")
    s = await env.settings.load()
    assert s.history_max_turns == 12 and s.ambient_threshold == 0.75

    await dash.post(
        "/behaviour/settings",
        {
            "history.max_turns": "8",
            "budget.daily_usd": "2.5",
            "memory.auto_approve": "on",
            "models.default": "claude-x",
        },
    )
    s = await env.settings.load()
    assert (s.history_max_turns, s.budget_daily_usd, s.memory_auto_approve, s.models.default) == (
        8,
        2.5,
        True,
        "claude-x",
    )


async def test_persona_history_and_restore(dash: Dash, env: Env) -> None:
    await dash.login()
    original = (await env.settings.load()).persona_system_prompt
    await dash.post("/behaviour/persona", {"persona": "You are a pirate."})
    assert (await env.settings.load()).persona_system_prompt == "You are a pirate."
    history = await env.db.read(lambda c: get_value(c, "persona.history"))
    assert history[-1]["text"] == original
    await dash.post("/behaviour/persona", {"persona": original})
    assert (await env.settings.load()).persona_system_prompt == original


async def test_ambient_settings_and_harvest_now(dash: Dash, env: Env) -> None:
    await dash.login()
    await dash.post(
        "/ambient/settings",
        {
            "ambient.threshold": "0.9",
            "ambient.enabled": "on",
            "ambient.mute_phrases": "bot shh\nquiet pls\n",
        },
    )
    s = await env.settings.load()
    assert s.ambient_threshold == 0.9 and s.ambient_mute_phrases == ["bot shh", "quiet pls"]
    assert s.harvest_enabled is False  # unchecked box → off
    await dash.post("/ambient/harvest")
    assert "nothing new to harvest" in await dash.flash("/ambient")


# --- categories ------------------------------------------------------------------------------


async def test_category_edit_rename_and_merge(dash: Dash, env: Env) -> None:
    makan = await seed_category(env, "makan", [("Pho", []), ("Laksa", [])])
    dinner = await seed_category(env, "dinner", [("Pho", [])])
    await env.db.write(
        lambda c: c.execute(
            "INSERT INTO decisions(category_id, option_id, choice_text, for_users, asked_by, "
            "status) VALUES (?, 1, 'Pho', 'both', ?, 'accepted')",
            (makan, env.jack.id),
        )
    )
    await dash.login()

    await dash.post(
        f"/categories/{dinner}",
        {
            "slug": "evening-meal",
            "display_name": "Evening meal",
            "description": "d",
            "recency_tau_days": "999",
            "default_n": "9",
        },
    )
    cat = await dash.stack.decisions.lookup("dinner")  # old slug kept as alias
    assert cat is not None and (
        cat.slug,
        cat.recency_tau_days,
        cat.default_n,
        cat.allow_generated,
    ) == ("evening-meal", 365.0, 5, False)

    await dash.post(f"/categories/{makan}/merge", {"into": str(dinner)})
    merged = await dash.stack.decisions.lookup("makan")
    assert merged is not None and merged.id == dinner
    opts = await dash.stack.decisions.list_options(merged)
    assert sorted(o.name for o in opts) == ["Laksa", "Pho"]  # duplicate Pho folded
    row = await env.db.read(
        lambda c: c.execute("SELECT category_id, option_id FROM decisions").fetchone()
    )
    assert row[0] == dinner and row[1] == 3  # repointed to the destination's Pho


async def test_option_crud_and_prefs_reset(dash: Dash, env: Env) -> None:
    cid = await seed_category(env, "dinner")
    await dash.login()
    await dash.post(
        f"/categories/{cid}/options",
        {"name": "Satay", "tags": "contains:peanut, grill", "owner": "jack"},
    )
    opt = await env.db.read(lambda c: c.execute("SELECT * FROM options").fetchone())
    assert (opt["name"], json.loads(opt["tags_json"]), opt["owner"]) == (
        "Satay",
        ["contains:peanut", "grill"],
        "jack",
    )
    await env.db.write(
        lambda c: c.execute("INSERT INTO option_prefs VALUES (?, ?, 2.0)", (opt["id"], env.jack.id))
    )
    await dash.post(
        f"/options/{opt['id']}",
        {
            "category_id": str(cid),
            "name": "Satay",
            "tags": "grill",
            "base_weight": "2",
            "owner": "shared",
        },
    )
    opt = await env.db.read(lambda c: c.execute("SELECT * FROM options").fetchone())
    assert (opt["active"], opt["base_weight"], opt["owner"]) == (0, 2.0, "shared")
    await dash.post(f"/options/{opt['id']}/reset-prefs", {"category_id": str(cid)})
    assert (
        await env.db.read(lambda c: c.execute("SELECT COUNT(*) FROM option_prefs").fetchone()[0])
        == 0
    )
    await dash.post(f"/categories/{cid}/aliases", {"alias": "Makan Where?"})
    assert (await dash.stack.decisions.lookup("makan where")).id == cid  # type: ignore[union-attr]


# --- memory ----------------------------------------------------------------------------------


async def test_note_edit_pin_delete(dash: Dash, env: Env) -> None:
    await dash.stack.store.write("memories/jack/food", mode="create", content="Likes laksa.")
    await dash.login()
    text = await dash.stack.store.read("memories/jack/food")
    assert text is not None
    raw = (env.vault / "memories/jack/food.md").read_text().replace("Likes laksa.", "Loves durian.")
    await dash.post("/memory/note", {"path": "memories/jack/food.md", "text": raw})
    hits = await dash.stack.memory.search("durian", asker="jack", scope="me")
    assert hits and hits[0].path == "memories/jack/food.md"
    await dash.post("/memory/note/pin", {"path": "memories/jack/food.md", "pinned": "on"})
    assert (await dash.stack.store.read("memories/jack/food")).pinned  # type: ignore[union-attr]
    await dash.post("/memory/note", {"path": "../escape.md", "text": "x"})
    assert "invalid" in (await dash.flash("/memory")).lower()
    await dash.post("/memory/note/delete", {"path": "memories/jack/food.md"})
    assert not await dash.stack.store.exists("memories/jack/food")


async def test_inbox_approve_via_htmx_and_suggestion(dash: Dash, env: Env) -> None:
    await dash.login()
    note = await dash.stack.memory.propose(
        owner="jack", content="Likes teh peng.", reason="r", topic="drinks", source="s"
    )
    sugg = await dash.stack.memory.suggest(
        kind="category",
        content="New: board games",
        reason="r",
        source="s",
        payload={"phrase": "board game", "aliases": [], "description": "d"},
    )
    r = await dash.post(
        f"/memory/inbox/{note.id}", {"action": "approve"}, headers={"HX-Request": "true"}
    )
    assert (
        r.status_code == 200
        and "approved" in r.text
        and r.text.startswith(f'<tr id="inbox-{note.id}"')
    )
    assert await dash.stack.store.exists("memories/jack/drinks")
    await dash.post(f"/memory/inbox/{sugg.id}", {"action": "approve"})
    assert await dash.stack.decisions.lookup("board game") is not None


# --- users & telegram ------------------------------------------------------------------------


async def test_answer_topic_switch_posts_hello(dash: Dash, env: Env) -> None:
    await dash.stack.topics.seen(GROUP_ID, 7, name="Tykee")
    await dash.login()
    await dash.post("/telegram/answer-topic", {"thread_id": "7"})
    assert await env.db.read(lambda c: get_value(c, KEY_ANSWER)) == 7
    sent = dash.stack.gateway.sent[-1]
    assert (sent.chat_id, sent.text, sent.thread_id) == (GROUP_ID, "I'll hang out here now 👋", 7)
    await dash.post("/telegram/answer-topic", {"thread_id": ""})
    assert await env.db.read(lambda c: get_value(c, KEY_ANSWER)) is None


async def test_answer_topic_switch_reports_dead_topic(dash: Dash) -> None:
    dash.stack.gateway.fail_threads = {8}
    await dash.login()
    await dash.post("/telegram/answer-topic", {"thread_id": "8"})
    assert dash.stack.health.answer_topic_error is not None
    assert "posting there failed" in await dash.flash("/users")


async def test_topic_settings_and_label(dash: Dash, env: Env) -> None:
    await dash.stack.topics.seen(GROUP_ID, 9)
    await dash.login()
    r = await dash.client.post(
        "/telegram/topics",
        data={"csrf": dash.csrf, "ignored": ["9", "11"], "off_topic_mention": "redirect"},
    )
    assert r.status_code == 303
    s = await env.settings.load()
    assert s.telegram_ignored_topic_ids == [9, 11] and s.telegram_off_topic_mention == "redirect"
    await dash.post("/telegram/topics/9/label", {"name": "Food"})
    assert await dash.stack.topics.name_of(GROUP_ID, 9) == "Food"


async def test_user_timezone_validation(dash: Dash, env: Env) -> None:
    await dash.login()
    await dash.post(f"/users/{env.jack.id}", {"display_name": "J", "timezone": "Mars/Base"})
    assert "Unknown timezone" in await dash.flash("/users")
    await dash.post(f"/users/{env.jack.id}", {"display_name": "J", "timezone": "Europe/London"})
    row = await env.db.read(
        lambda c: c.execute("SELECT * FROM users WHERE id = ?", (env.jack.id,)).fetchone()
    )
    assert (row["display_name"], row["timezone"]) == ("J", "Europe/London")


async def test_system_reindex(dash: Dash) -> None:
    await dash.stack.store.write("memories/jack/a", mode="create", content="alpha")
    await dash.login()
    await dash.post("/system/reindex")
    assert "Reindexed 1 notes" in await dash.flash("/system")


async def test_conversation_shows_tool_calls(dash: Dash, env: Env) -> None:
    from tests.conftest import mention
    from tests.fakes.fake_llm import tool_call

    dash.stack.llm._replies.extend([tool_call("list_options", category="dinner"), "ok"])
    body, ents = mention("dinner?")
    await dash.stack.adapter.handle_message(tg_message(body, entities=ents), env.jack)
    await dash.login()
    html = (await dash.client.get(f"/conversations/{GROUP_ID}")).text
    assert "list_options" in html and "@TykeeBot dinner?" in html and "Tykee</span>: ok" in html
    _ = set_value


async def test_answer_topic_rejects_junk_and_ignored_topics(dash: Dash, env: Env) -> None:
    await env.db.write(lambda c: set_value(c, "telegram.ignored_topic_ids", [9]))
    await dash.login()
    r = await dash.post("/telegram/answer-topic", {"thread_id": "abc"})
    assert r.status_code == 303 and "Not saved" in await dash.flash("/users")
    await dash.post("/telegram/answer-topic", {"thread_id": "9"})
    assert "ignored" in await dash.flash("/users")
    assert await env.db.read(lambda c: get_value(c, KEY_ANSWER)) is None
    assert dash.stack.gateway.sent == []


async def test_topic_settings_reject_junk_and_ignoring_the_answer_topic(
    dash: Dash, env: Env
) -> None:
    await env.db.write(lambda c: set_value(c, KEY_ANSWER, 5))
    await dash.login()
    r = await dash.client.post("/telegram/topics", data={"csrf": dash.csrf, "ignored": ["x"]})
    assert r.status_code == 303 and "must be numbers" in await dash.flash("/users")
    await dash.client.post("/telegram/topics", data={"csrf": dash.csrf, "ignored": ["5"]})
    assert "be ignored" in await dash.flash("/users")
    assert (await env.settings.load()).telegram_ignored_topic_ids == []


async def test_note_redirects_encode_the_path(dash: Dash) -> None:
    await dash.login()
    r = await dash.post(
        "/memory/note", {"path": "memories/jack/咖啡", "text": "---\ntitle: Kopi\n---\nbody\n"}
    )
    assert r.headers["location"] == "/memory/note?path=memories/jack/%E5%92%96%E5%95%A1.md"
    assert (await dash.client.get(r.headers["location"])).status_code == 200


# --- import wizard (§15.2) ----------------------------------------------------------------------


async def test_import_wizard_end_to_end(dash: Dash) -> None:
    await dash.login()
    svc = dash.deps.importer
    assert svc is not None
    await dash.stack.store.ensure_skeleton(dash.deps.users)
    files = {"export": ("result.json", dumps(single_chat(dinner_chat())), "application/json")}
    r = await dash.post("/import/upload", files=files)
    assert r.status_code == 303 and r.headers["location"] == "/import/1"
    page = (await dash.client.get("/import/1")).text
    assert "Telegram single-chat export: <b>Jack &amp; Sam</b>, 12 messages" in page
    assert 'name="sender:user222"' in page and "Preview &amp; estimate cost" in page

    again = await dash.post("/import/upload", files=files)
    assert again.headers["location"] == "/import/1"
    assert "already uploaded" in await dash.flash("/import/1")
    bad = {"export": ("x.json", b'{"nope": 1}', "application/json")}
    await dash.post("/import/upload", files=bad)
    assert "isn&#39;t a Telegram export" in await dash.flash("/import")

    form = {"chats": "4242", "since": "2026-04-02", "until": "2026-10-02",
            "sender:user111": "jack", "sender:user222": "partner"}  # fmt: skip
    await dash.post("/import/1/configure", form)
    page = (await dash.client.get("/import/1")).text
    assert "Preview &amp; cost" in page and "partner: not mala again lah" in page
    assert "Both participants agreed" in page

    await dash.post("/import/1/start")  # no consent
    assert "consent box" in await dash.flash("/import/1")
    r = await dash.post("/import/1/start", {"consent": "on"})
    assert r.status_code == 303
    if svc._kick is not None:
        await svc._kick
    assert (await svc.job(1)).status == "extracting"
    page = (await dash.client.get("/import/1")).text
    assert "0 / 4 windows" in page and 'hx-trigger="every 5s"' in page
    dash.stack.clock.advance(minutes=5)
    await svc.tick()
    job = await svc.job(1)
    assert job.status == "review", job.error
    progress = await dash.client.get("/import/1/progress", headers={"HX-Request": "true"})
    assert progress.headers.get("hx-redirect") == "/import/1"

    page = (await dash.client.get("/import/1")).text
    assert "Review the <b>categories first</b>" in page and "makan where" in page
    cats = await svc.review(1, "category")
    edit = {"slug": "dinner", "display_name": "Dinner time", "description": "eat",
            "recency_tau_days": "2", "default_n": "1",
            "aliases": "makan where, 晚餐, dinner"}  # fmt: skip
    await dash.post(f"/import/1/categories/{cats[0].id}", edit)
    assert (await svc.review(1, "category"))[0].payload["display_name"] == "Dinner time"
    for tab in ("categories", "options", "decisions", "notes"):
        await dash.post("/import/1/bulk", {"tab": tab, "min_conf": "0.8"})
        assert (await dash.client.get(f"/import/1?tab={tab}")).status_code == 200
    (note,) = await svc.review(1, "note")
    await dash.post(f"/import/1/notes/{note.id}", {"text": "- Finds mala too heavy\n\nLikes YTF"})
    assert [ln["text"] for ln in (await svc.review(1, "note"))[0].payload["lines"]] == [
        "Finds mala too heavy",
        "Likes YTF",
    ]
    r = await dash.post("/import/1/apply", {"delete_export": "on"})
    assert "Applied: 1 categories, 2 options, 4 decisions, 1 notes" in await dash.flash("/import/1")
    assert (await svc.job(1)).status == "done"
    assert "done" in (await dash.client.get("/import")).text


async def test_import_upload_size_limit(dash: Dash, env: Env) -> None:
    await dash.login()
    await env.db.write(lambda c: set_value(c, "import.max_upload_mb", 1))
    big = {"export": ("result.json", b"x" * (3 * 1024 * 1024), "application/json")}
    await dash.post("/import/upload", files=big)
    assert "Too big" in await dash.flash("/import")

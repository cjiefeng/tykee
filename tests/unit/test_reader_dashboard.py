"""System → Account reader page (§10.7): step-up auth, alerts, consent, remove, Disconnect."""

from __future__ import annotations

from pathlib import Path

from app.reader.models import ChatInfo, DialogName
from tests.conftest import GROUP_ID, PARTNER_TG, Env
from tests.fakes.fake_reader import FakeReader
from tests.unit.test_dashboard import PASSWORD, Dash, dash  # noqa: F401  (fixture)
from tests.unit.test_reader import Rig, msg, reader_on

PAGE = "/system/reader"


async def _rig(d: Dash, env: Env, tmp_path: Path) -> Rig:
    fake = FakeReader(
        entities={"partner_t": ChatInfo(PARTNER_TG, 99, "user", "Partner")},
        dialogs=[
            DialogName(PARTNER_TG, "Partner", "user"),
            DialogName(GROUP_ID, "Us", "group"),
            DialogName(-1001, "News", "channel"),
        ],
        history={PARTNER_TG: [msg(1, "hi")]},
    )
    await reader_on(env)
    r = Rig(env, d.stack, fake, tmp_path)
    d.deps.reader = r.service
    await d.login()
    return r


async def test_page_renders_without_reader(dash: Dash) -> None:  # noqa: F811
    await dash.login()
    page = await dash.client.get(PAGE)
    assert page.status_code == 200 and "Not configured" in page.text
    system = await dash.client.get("/system")
    assert "/system/reader" in system.text


async def test_add_needs_password_and_alerts(dash: Dash, env: Env, tmp_path: Path) -> None:  # noqa: F811
    r = await _rig(dash, env, tmp_path)
    form = {"ref": "@partner_t", "label": "DM", "consent": "on", "password": "wrong"}
    await dash.post(PAGE + "/add", form)
    assert "Wrong password" in await dash.flash(PAGE)
    assert await r.service.chats() == [] and r.alerts == []
    await dash.post(PAGE + "/add", {**form, "password": PASSWORD})
    chats = await r.service.chats()
    assert [(c.label, c.readable) for c in chats] == [("DM", True)]
    assert r.alerts == ["Tykee reader: 'DM' added to readable chats"]
    html = await dash.flash(PAGE)
    assert "DM" in html and "add" in html and "Disconnect" in html
    # Guard rail: Tykee's own group.
    r.fake.entities[GROUP_ID] = ChatInfo(GROUP_ID, 1, "group", "Us", members=2)
    await dash.post(PAGE + "/add", {"ref": str(GROUP_ID), "password": PASSWORD})
    assert "own group" in await dash.flash(PAGE)


async def test_consent_update_remove(dash: Dash, env: Env, tmp_path: Path) -> None:  # noqa: F811
    r = await _rig(dash, env, tmp_path)
    await dash.post(PAGE + "/add", {"ref": "partner_t", "password": PASSWORD})
    cid = (await r.service.chats())[0].id
    assert "Tick consent" in await dash.flash(PAGE)
    await dash.post(f"{PAGE}/{cid}/consent", {"consent": "on", "password": "nope"})
    assert (await r.service.chat(cid)).consent_at is None
    await dash.post(f"{PAGE}/{cid}/consent", {"consent": "on", "password": PASSWORD})
    assert (await r.service.chat(cid)).consent_at is not None
    await dash.post(f"{PAGE}/{cid}/consent", {})  # withdraw: no password needed
    assert (await r.service.chat(cid)).consent_at is None
    await dash.post(
        f"{PAGE}/{cid}/update",
        {"label": "Partner DM", "enabled": "on", "interval_min": "2", "retention_days": "7"},
    )
    assert "5 to 1440" in await dash.flash(PAGE)
    await dash.post(
        f"{PAGE}/{cid}/update",
        {"label": "Partner DM", "enabled": "on", "interval_min": "15", "retention_days": "3"},
    )
    ch = await r.service.chat(cid)
    assert (ch.label, ch.interval_min, ch.retention_days) == ("Partner DM", 15, 3)
    await dash.post(f"{PAGE}/{cid}/remove")
    assert await r.service.chats() == []


async def test_pick_and_disconnect(dash: Dash, env: Env, tmp_path: Path) -> None:  # noqa: F811
    r = await _rig(dash, env, tmp_path)
    html = (await dash.client.get(PAGE + "/pick")).text
    assert f"ref={PARTNER_TG}" in html and html.count("not allowed") == 2
    await dash.post(PAGE + "/disconnect")
    assert r.fake.logged_out and not r.vault.exists()
    assert "Disconnected" in await dash.flash(PAGE)
    await dash.post(PAGE + "/enabled", {"enabled": "on"})
    assert "log in first" in await dash.flash(PAGE)

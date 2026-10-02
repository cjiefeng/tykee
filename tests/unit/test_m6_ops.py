"""M6: scheduled nudges (§10.3), backups (§14.2), healthcheck + watchdog (§14.1, §14.4)."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.ambient import state as ambient_state
from app.backup import BackupService, _git, commit_vault, list_backups, prune
from app.health import HEARTBEAT_FILE, HealthState, Watchdog, heartbeat
from app.healthcheck import check_heartbeat
from app.nudges import NudgeService, is_due, nudge_text
from app.settings import Nudge, RuntimeSettings, set_value
from app.telegram.topics import KEY_ANSWER
from tests.conftest import GROUP_ID, JACK_TG, NOW, TZ, Env, Stack, make_stack, seed_category

ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


async def _set(env: Env, values: dict[str, Any]) -> None:
    def _w(c: sqlite3.Connection) -> None:
        for k, v in values.items():
            set_value(c, k, v)

    await env.db.write(_w)


def _nudge(**kw: Any) -> dict[str, Any]:
    return {"id": "dinner", "time": "19:00", "days": ALL_DAYS, "category": "dinner", **kw}


def _service(env: Env, stack: Stack, group: int | None = GROUP_ID) -> NudgeService:
    return NudgeService(
        db=env.db,
        settings=env.settings,
        decisions=stack.decisions,
        sender=stack.adapter,
        users=env.users,
        tz=TZ,
        group_id=lambda: group,
        clock=stack.clock,
    )


async def _runs(env: Env) -> list[tuple[str, str, str | None]]:
    rows = await env.db.read(
        lambda c: c.execute(
            "SELECT nudge_id, status, reason FROM nudge_runs ORDER BY id"
        ).fetchall()
    )
    return [(r[0], r[1], r[2]) for r in rows]


# --- settings --------------------------------------------------------------------------------


def test_nudge_validation() -> None:
    n = Nudge.model_validate(_nudge(time="7:05", days=["Fri", "mon", "monday"]))
    assert n.time == "07:05" and n.days == ["mon", "fri"]
    for bad in ({"time": "25:00"}, {"time": "noon"}, {"days": []}, {"days": ["xyz"]}):
        with pytest.raises(ValidationError):
            Nudge.model_validate(_nudge(**bad))
    with pytest.raises(ValidationError, match="unique"):
        RuntimeSettings.model_validate(
            {"persona_system_prompt": "p", "models": _models(), "nudges_items": [_nudge()] * 2}
        )
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(
            {"persona_system_prompt": "p", "models": _models(), "backup_time": "3am"}
        )


def _models() -> dict[str, str]:
    roles = ("default", "escalated", "judge", "import_extract", "import_consolidate")
    return dict.fromkeys(roles, "m")


async def test_seeds_nudges_off_backups_on(env: Env) -> None:
    s = await env.settings.load()
    assert s.nudges_enabled is False and s.nudges_items == []
    assert s.backup_enabled and s.backup_time == "03:00" and s.backup_keep == 14


def test_is_due_window() -> None:
    n = Nudge.model_validate(_nudge(time="17:30", days=["fri"]))
    fri = datetime(2026, 10, 2, 17, 30, tzinfo=TZ)  # a Friday
    assert is_due(n, fri, 30)
    assert is_due(n, fri + timedelta(minutes=29), 30)
    assert not is_due(n, fri + timedelta(minutes=30), 30)
    assert not is_due(n, fri - timedelta(minutes=1), 30)
    assert not is_due(n, fri + timedelta(days=1), 30)  # Saturday


def test_nudge_text() -> None:
    assert nudge_text("Dinner", "Thai", None) == "🎲 Dinner? I'm thinking **Thai**"
    assert nudge_text("Dinner", "Thai", 1) == "🎲 Dinner? I'm thinking **Thai**"
    assert nudge_text("Dinner", "Thai", 9).endswith("(last time was 9 days ago)")


# --- nudges ----------------------------------------------------------------------------------


async def test_nudge_off_by_default_does_nothing(env: Env) -> None:
    stack = make_stack(env)
    await seed_category(env, "dinner", [("Thai", [])])
    await _set(env, {"nudges.items": [_nudge()]})
    assert await _service(env, stack).tick() == []
    assert stack.gateway.sent == []


async def test_nudge_sends_once_into_answer_topic(env: Env) -> None:
    stack = make_stack(env)
    await seed_category(env, "dinner", [("Thai", [])])
    await _set(env, {"nudges.enabled": True, "nudges.items": [_nudge()], KEY_ANSWER: 42})
    svc = _service(env, stack)
    stack.clock.advance(minutes=5)  # 19:05, inside the grace window
    [o] = await svc.tick()
    assert o.status == "sent" and o.decision_id
    [sent] = stack.gateway.sent
    assert sent.chat_id == GROUP_ID and sent.thread_id == 42
    assert sent.text == "🎲 Dinner? I'm thinking **Thai**"
    assert sent.keyboard  # ✅🎲❌ buttons, like any pick
    stored = await env.db.read(
        lambda c: c.execute(
            "SELECT content, thread_id FROM messages WHERE role = 'assistant'"
        ).fetchone()
    )
    assert stored["thread_id"] == 42  # in history, so "sure!" has context
    stack.clock.advance(minutes=1)
    assert await svc.tick() == []  # once per day
    assert len(stack.gateway.sent) == 1
    assert await _runs(env) == [("dinner", "sent", None)]


async def test_nudge_outside_window_or_wrong_day(env: Env) -> None:
    stack = make_stack(env)
    await seed_category(env, "dinner", [("Thai", [])])
    await _set(env, {"nudges.enabled": True, "nudges.items": [_nudge(time="18:00")]})
    assert await _service(env, stack).tick() == []  # 19:00 is past 18:00 + 30 min grace
    await _set(env, {"nudges.items": [_nudge(days=["mon"])]})  # NOW is a Friday
    assert await _service(env, stack).tick() == []


async def test_nudge_skips_when_muted_or_decided(env: Env) -> None:
    stack = make_stack(env)
    await seed_category(env, "dinner", [("Thai", []), ("Pho", [])])
    await _set(env, {"nudges.enabled": True, "nudges.items": [_nudge(), _nudge(id="d2")]})
    await env.db.write(lambda c: ambient_state.set_mute(c, GROUP_ID, NOW + timedelta(hours=1)))
    svc = _service(env, stack)
    outcomes = await svc.tick()
    assert [(o.status, o.reason) for o in outcomes] == [("skipped", "group muted")] * 2
    assert stack.gateway.sent == []

    # Next day, unmuted, but someone already asked for dinner in the group.
    await env.db.write(lambda c: ambient_state.set_mute(c, GROUP_ID, None))
    stack.clock.advance(days=1)
    cat = await stack.decisions.lookup("dinner")
    assert cat is not None
    from app.decisions.engine import PickRequest

    await stack.decisions.pick(
        cat, PickRequest(cat.id, "both"), asked_by=env.jack.id, chat_id=GROUP_ID
    )
    o, _ = await svc.tick()
    assert (o.status, o.reason) == ("skipped", "Dinner already decided today")


async def test_nudge_skip_reasons_and_dm_target(env: Env) -> None:
    stack = make_stack(env)
    await seed_category(env, "dinner", [("Thai", [])])
    await seed_category(env, "movie")  # no options
    await _set(
        env,
        {
            "nudges.enabled": True,
            "nudges.items": [
                _nudge(id="a", category="nope"),
                _nudge(id="b", category="movie"),
                _nudge(id="c", target="ghost"),
                _nudge(id="d", target="jack"),
            ],
        },
    )
    outcomes = await _service(env, stack).tick()
    assert [(o.nudge_id, o.status, o.reason) for o in outcomes] == [
        ("a", "skipped", "unknown category 'nope'"),
        ("b", "skipped", "no options for Movie"),
        ("c", "skipped", "unknown target 'ghost'"),
        ("d", "sent", ""),
    ]
    [dm] = stack.gateway.sent
    assert dm.chat_id == JACK_TG and dm.thread_id is None
    assert await _service(env, stack, group=None).run_now("dinner") is not None


async def test_nudge_no_group_and_failed_send(env: Env) -> None:
    stack = make_stack(env)
    await seed_category(env, "dinner", [("Thai", [])])
    await _set(env, {"nudges.enabled": True, "nudges.items": [_nudge()], KEY_ANSWER: 42})
    [o] = await _service(env, stack, group=None).tick()
    assert (o.status, o.reason) == ("skipped", "no group configured")
    stack.clock.advance(days=1)
    stack.gateway.fail_threads = {42}  # answer topic deleted
    [o] = await _service(env, stack).tick()
    assert o.status == "failed"
    assert stack.health.answer_topic_error  # red tile, as for any answer-topic send


async def test_nudge_mentions_days_since_last_accepted(env: Env) -> None:
    stack = make_stack(env)
    cid = await seed_category(env, "dinner", [("Thai", [])])
    old = (NOW - timedelta(days=9)).strftime("%Y-%m-%d %H:%M:%S")
    await env.db.write(
        lambda c: c.execute(
            "INSERT INTO decisions(category_id, option_id, choice_text, for_users, asked_by, "
            "status, chat_id, created_at) VALUES (?, 1, 'Thai', 'both', ?, 'accepted', ?, ?)",
            (cid, env.jack.id, GROUP_ID, old),
        )
    )
    await _set(env, {"nudges.enabled": True, "nudges.items": [_nudge()]})
    await _service(env, stack).tick()
    assert stack.gateway.sent[0].text.endswith("(last time was 9 days ago)")


async def test_run_now_ignores_rules_and_counts_as_today(env: Env) -> None:
    stack = make_stack(env)
    await seed_category(env, "dinner", [("Thai", [])])
    await _set(env, {"nudges.items": [_nudge(time="08:00")]})  # master toggle off, not due
    svc = _service(env, stack)
    o = await svc.run_now("dinner")
    assert o.status == "sent" and len(stack.gateway.sent) == 1
    assert (await svc.run_now("missing")).status == "failed"
    await _set(env, {"nudges.enabled": True, "nudges.items": [_nudge()]})
    assert await svc.tick() == []  # already ran today


# --- backups ---------------------------------------------------------------------------------


def _backups(env: Env, stack: Stack, tmp: Path, health: HealthState | None = None) -> BackupService:
    return BackupService(
        db=env.db,
        settings=env.settings,
        backup_dir=tmp / "backups",
        vault=env.vault,
        tz=TZ,
        health=health,
        clock=stack.clock,
    )


async def test_backup_copy_is_consistent(env: Env, tmp_path: Path) -> None:
    stack = make_stack(env)
    await seed_category(env, "dinner", [("Thai", [])])
    health = HealthState()
    r = await _backups(env, stack, tmp_path, health).run()
    assert r.db_file == "bot-20261002.db"
    copy = sqlite3.connect(tmp_path / "backups" / r.db_file)
    assert copy.execute("SELECT slug FROM categories").fetchall() == [("dinner",)]
    assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    copy.close()
    assert health.last_backup_at is not None and health.last_backup_error is None
    assert not list((tmp_path / "backups").glob(".*.tmp"))


async def test_backup_tick_once_per_day_after_time(env: Env, tmp_path: Path) -> None:
    stack = make_stack(env)
    svc = _backups(env, stack, tmp_path)
    await _set(env, {"backup.time": "20:00"})
    assert await svc.tick() is None  # 19:00 local: not yet
    await _set(env, {"backup.time": "03:00"})
    assert await svc.tick() is not None
    assert await svc.tick() is None  # today's file exists
    stack.clock.advance(days=1)
    await _set(env, {"backup.enabled": False})
    assert await svc.tick() is None
    assert [b.name for b in list_backups(tmp_path / "backups")] == ["bot-20261002.db"]


def test_prune_keeps_newest(tmp_path: Path) -> None:
    for day in range(1, 6):
        (tmp_path / f"bot-202610{day:02d}.db").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("not a backup")
    assert prune(tmp_path, 2) == ["bot-20261003.db", "bot-20261002.db", "bot-20261001.db"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "bot-20261004.db",
        "bot-20261005.db",
        "notes.txt",
    ]


@needs_git
async def test_vault_commit(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "people").mkdir(parents=True)
    (vault / "people" / "jack.md").write_text("hi")
    assert await commit_vault(vault, "nightly 1") == "committed"
    assert await commit_vault(vault, "nightly 2") == "unchanged"
    (vault / "people" / "jack.md").unlink()
    assert await commit_vault(vault, "nightly 3") == "committed"  # deletions are committed too
    _, log = await _git(vault, "log", "--format=%s")
    assert log.split("\n")[:2] == ["nightly 3", "nightly 1"]
    assert (await _git(vault, "remote", "add", "origin", "https://x/y"))[0] == 0
    (vault / "new.md").write_text("x")
    assert (await commit_vault(vault, "nightly 4")).startswith("failed: vault repo has a remote")


async def test_vault_commit_without_vault(tmp_path: Path) -> None:
    status = await commit_vault(tmp_path / "missing", "x")
    assert status.startswith("skipped")


# --- health ----------------------------------------------------------------------------------


def test_check_heartbeat(tmp_path: Path) -> None:
    assert check_heartbeat(tmp_path) == (False, "no heartbeat file")
    beat = tmp_path / HEARTBEAT_FILE
    beat.touch()
    assert check_heartbeat(tmp_path)[0]
    old = time.time() - 600
    os.utime(beat, (old, old))
    assert not check_heartbeat(tmp_path)[0]


async def test_heartbeat_touches_file(tmp_path: Path) -> None:
    health = HealthState()
    health.loop_beat = 0
    task = asyncio.create_task(heartbeat(health, tmp_path / HEARTBEAT_FILE, interval_s=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    assert health.loop_beat > 0 and (tmp_path / HEARTBEAT_FILE).exists()


def test_watchdog_fires_on_stalled_loop() -> None:
    health = HealthState()
    fired: list[bool] = []
    dog = Watchdog(health, stall_s=0.05, check_s=0.01, on_stall=lambda: fired.append(True))
    dog.start()
    time.sleep(0.3)
    dog.stop()
    assert fired == [True]


def test_watchdog_quiet_while_beating() -> None:
    health = HealthState()
    fired: list[bool] = []
    dog = Watchdog(health, stall_s=0.2, check_s=0.01, on_stall=lambda: fired.append(True))
    dog.start()
    for _ in range(20):
        health.loop_beat = time.monotonic()
        time.sleep(0.02)
    dog.stop()
    assert fired == []


def test_scheduler_ok_grace() -> None:
    h = HealthState()
    assert h.scheduler_ok()  # just started
    h.started_at -= timedelta(minutes=10)
    assert not h.scheduler_ok()
    h.last_tick_at = datetime.now(h.started_at.tzinfo)
    assert h.scheduler_ok()

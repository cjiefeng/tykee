"""Bootstrap import building blocks (§15): Telegram parser, zip guard, windowing, estimate, and
the deterministic consolidation steps (§15.3, §15.3.2)."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.extraction.schema import ConsideredOption, Episode, Fact, OptionSeen
from app.importer import consolidate as cs
from app.importer import telegram as tg
from app.importer.windowing import (
    NEW_MARKER,
    build_windows,
    covered,
    estimate_cost,
    estimate_tokens,
)
from app.settings import Pricing
from tests.fakes.telegram_export import T0, account, dumps, msg, partner, single_chat

TZ = ZoneInfo("Asia/Singapore")


def _write(tmp_path: Path, export: dict[str, object], name: str = "result.json") -> Path:
    p = tmp_path / name
    p.write_bytes(dumps(export))
    return p


# --- parser ----------------------------------------------------------------------------------


def test_single_chat_normalises_text_media_and_forwards(tmp_path: Path) -> None:
    export = single_chat(
        [
            {"id": 1, "type": "service", "date": "2026-07-03T18:00:00", "action": "pin_message"},
            msg(2, "plain"),
            msg(3, ["see ", {"type": "link", "text": "this place"}, "!"]),
            partner(4, "", photo="photos/p.jpg"),
            partner(5, "", media_type="sticker", sticker_emoji="😋"),
            msg(6, "lol", forwarded_from="Food Channel"),
            msg(7, "", media_type="voice_message"),
            msg(8, ""),  # nothing left → skipped
        ]
    )
    out = list(tg.parse(_write(tmp_path, export), TZ))
    assert [m.text for m in out] == [
        "plain",
        "see this place!",
        "[photo]",
        "[sticker 😋]",
        "[fwd] lol",
        "[voice note]",
    ]
    assert out[0].chat_ref == "4242" and out[0].sender_ref == "user111"
    assert out[0].ts == T0 + timedelta(minutes=2)


def test_naive_date_without_unixtime_is_household_local(tmp_path: Path) -> None:
    m = {"id": 1, "type": "message", "date": "2026-07-03T19:00:00", "from": "J",
         "from_id": "user111", "text": "hi"}  # fmt: skip
    (out,) = tg.parse(_write(tmp_path, single_chat([m])), TZ)
    assert out.ts == datetime(2026, 7, 3, 11, 0, tzinfo=UTC)


def test_scan_summarises_chats_and_senders(tmp_path: Path) -> None:
    path = _write(tmp_path, single_chat([msg(1, "a"), partner(2, "b"), partner(3, "c")]))
    s = tg.scan(path, TZ)
    assert s.format == "single" and s.messages == 3
    (chat,) = s.chats
    assert chat.name == "Jack & Sam"
    assert {k: (v.name, v.count) for k, v in chat.senders.items()} == {
        "user111": ("Jack", 1),
        "user222": ("Sam", 2),
    }
    assert chat.first == T0 + timedelta(minutes=1) and chat.last == T0 + timedelta(minutes=3)


def test_account_export_lists_chats_and_filters(tmp_path: Path) -> None:
    export = account(
        [
            {"name": "Sam", "type": "personal_chat", "id": 1, "messages": [partner(1, "hi")]},
            {"name": "Us", "type": "private_supergroup", "id": 2,
             "messages": [msg(1, "dinner?"), partner(2, "ytf")]},
            {"name": "Empty", "type": "saved_messages", "id": 3, "messages": []},
        ]
    )  # fmt: skip
    path = _write(tmp_path, export)
    s = tg.scan(path, TZ)
    assert s.format == "account"
    assert [(c.ref, c.name, c.count) for c in s.chats] == [("2", "Us", 2), ("1", "Sam", 1)]
    assert [m.text for m in tg.parse(path, TZ, {"2"})] == ["dinner?", "ytf"]


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"{not json", "not valid JSON"),
        (b'{"hello": 1}', "isn't a Telegram export"),
        (dumps(single_chat([{"id": 1, "type": "service", "date": "2026-07-03T18:00:00"}])),
         "no text messages"),
    ],
)  # fmt: skip
def test_scan_rejects_non_exports(tmp_path: Path, data: bytes, error: str) -> None:
    p = tmp_path / "x.json"
    p.write_bytes(data)
    with pytest.raises(tg.ExportError, match=error):
        tg.scan(p, TZ)


def test_zip_extracts_only_result_json(tmp_path: Path) -> None:
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ChatExport/photos/p.jpg", b"\xff" * 100)
        zf.writestr("ChatExport/result.json", dumps(single_chat([msg(1, "hi " * 50)])))
    assert tg.is_zip(z)
    tg.extract_result_json(z, tmp_path / "out.json")
    assert tg.scan(tmp_path / "out.json", TZ).messages == 1


def test_zip_bomb_and_missing_json_are_refused(tmp_path: Path) -> None:
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("result.json", b" " * 5_000_000)  # compresses ~1000x
    with pytest.raises(tg.ExportError, match="suspiciously"):
        tg.extract_result_json(bomb, tmp_path / "o.json")
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", b"hi")
    with pytest.raises(tg.ExportError, match=r"no result\.json"):
        tg.extract_result_json(empty, tmp_path / "o.json")


def test_copy_limited_stops_at_the_limit(tmp_path: Path) -> None:
    with pytest.raises(tg.ExportError, match="larger than"):
        tg.copy_limited(io.BytesIO(b"x" * 3_000_000), tmp_path / "u", 2 * 1024 * 1024)
    assert not (tmp_path / "u").exists()


# --- windowing -------------------------------------------------------------------------------


def _nm(i: int, text: str, at: datetime, chat: str = "c1") -> tg.NormalisedMessage:
    return tg.NormalisedMessage(chat, i, at, "user111", "x", text)


def test_windows_split_on_gap_chat_and_size_with_overlap() -> None:
    msgs = [_nm(i, f"line {i}", T0 + timedelta(minutes=i)) for i in range(1, 4)]
    msgs.append(_nm(4, "much later", T0 + timedelta(hours=5)))
    msgs.append(_nm(1, "other chat", T0 + timedelta(hours=5, minutes=1), chat="c2"))
    labels = {"user111": "jack"}
    ws = list(build_windows(msgs, labels, TZ))
    assert [(w.chat_ref, w.first_msg_id, w.last_msg_id) for w in ws] == [
        ("c1", 1, 3),
        ("c1", 4, 4),
        ("c2", 1, 1),
    ]
    assert ws[0].lines[0] == "[2026-07-03 19:01] jack: line 1"
    assert not ws[1].context  # a gap split starts fresh

    long = [_nm(i, "word " * 200, T0 + timedelta(minutes=i)) for i in range(1, 30)]
    ws = list(build_windows(long, {}, TZ, max_tokens=1000))
    assert len(ws) > 3
    assert ws[1].context == ws[0].lines[-10:]
    assert NEW_MARKER in ws[1].text and "other:" in ws[1].text
    assert sum(w.msg_count for w in ws) == 29


def test_covered_and_estimates() -> None:
    m = _nm(15, "x", T0)
    assert covered(m, {"c1": [(10, 20)]}) and not covered(m, {"c1": [(16, 20)], "c2": [(1, 99)]})
    assert estimate_tokens("a" * 35) == 10 and estimate_tokens("晚餐吃什么") == 5
    price = Pricing(input=4.0, output=20.0, cache_write=5.0, cache_read=0.2)
    est = estimate_cost([6000] * 100, 1000, price, price, 0.5)
    assert est.input_tokens == 700_000
    assert est.extract_usd == pytest.approx(0.5 * (0.7 * 4 + 0.15 * 20))
    assert 1.0 < est.total_usd < 10.0


# --- consolidation ---------------------------------------------------------------------------


def _ep(
    phrase: str, choice: str = "", conf: float = 0.9, opts: list[ConsideredOption] | None = None
) -> Episode:
    return Episode(
        summary=f"deciding {phrase}",
        category_phrase=phrase,
        phrases_seen=[phrase],
        for_users="both",
        options_considered=opts or [],
        outcome="chosen" if choice else "undecided",
        choice=choice,
        quotes=[f"{phrase}?"],
        confidence=conf,
    )


def _recs(*eps: Episode) -> dict[str, cs.EpisodeRec]:
    return {
        f"E{i}": cs.EpisodeRec(f"w{i}-e1", ep, T0 + timedelta(days=i))
        for i, ep in enumerate(eps, 1)
    }


def test_dedupe_merges_the_same_episode_from_overlapping_windows() -> None:
    a = cs.EpisodeRec("w1-e1", _ep("dinner", "YTF", 0.7), T0)
    b = cs.EpisodeRec(
        "w2-e1",
        _ep("Dinner", "ytf", 0.9).model_copy(update={"quotes": ["ok ytf"]}),
        T0 + timedelta(hours=1),
    )
    c = cs.EpisodeRec("w3-e1", _ep("dinner", "YTF"), T0 + timedelta(days=1))
    out = cs.dedupe_episodes([a, b, c])
    assert [r.id for r in out] == ["w2-e1", "w3-e1"]
    assert out[0].ep.quotes == ["ok ytf", "dinner?"]


def test_validate_design_enforces_the_rules() -> None:
    recs = _recs(
        _ep("dinner", "YTF"), _ep("makan where", "Thai"), _ep("dinner", "Ramen"),
        _ep("movie", "Dune"),  # 1 episode → below support
        _ep("salary talk"),  # out of scope
        _ep("lunch", "Bak chor mee"),  # not mentioned by the model at all
        _ep("weekend", "Hike"), _ep("weekend", "Beach"),  # 2 chosen → enough
    )  # fmt: skip
    raw = {
        "categories": [
            {"slug": "Dinner", "display_name": "Dinner", "description": "what to eat",
             "recency_tau_days": 0.1, "default_n": 9, "allow_generated": True,
             "aliases": ["makan where", "晚餐", "plans"], "episode_ids": ["E1", "E2", "E99"]},
            {"slug": "dinner", "display_name": "dup", "description": "", "recency_tau_days": 3,
             "default_n": 1, "allow_generated": True, "aliases": ["dinner tonight"],
             "episode_ids": ["E3", "E1"]},
            {"slug": "movie", "display_name": "Movie", "description": "", "recency_tau_days": 60,
             "default_n": 3, "allow_generated": True, "aliases": [], "episode_ids": ["E4"]},
            {"slug": "weekend-activity", "display_name": "Weekend", "description": "",
             "recency_tau_days": 14, "default_n": 1, "allow_generated": False,
             "aliases": ["plans", "weekend"], "episode_ids": ["E7", "E8"]},
        ],
        "unmapped": [{"episode_id": "E5", "why": "out_of_scope"}],
    }  # fmt: skip
    design = cs.validate_design(raw, recs, [], {"weekend": "outings"})
    by = {c.slug: c for c in design.categories}
    assert set(by) == {"dinner", "weekend-activity"}
    dinner = by["dinner"]
    assert dinner.episode_ids == ["w1-e1", "w2-e1", "w3-e1"]  # E99 ignored, E1 only once
    assert dinner.recency_tau_days == 0.5 and dinner.default_n == 5
    assert "makan where" in dinner.aliases and "dinner tonight" in dinner.aliases
    assert "plans" not in dinner.aliases and "plans" not in by["weekend-activity"].aliases
    assert any("plans" in f for f in dinner.flags)
    assert "weekend" not in by["weekend-activity"].aliases  # belongs to an existing category
    assert design.out_of_scope == 1
    unmapped = dict(design.unmapped)
    assert "too few" in unmapped["w4-e1"] and unmapped["w6-e1"] == "not assigned to a category"
    assert "w5-e1" not in unmapped  # out-of-scope episodes are discarded, not listed


def test_validate_design_maps_into_existing_category() -> None:
    recs = _recs(_ep("dinner", "YTF"))
    raw = {"categories": [{"slug": "dinner", "display_name": "Evening meal", "description": "x",
                           "recency_tau_days": 3, "default_n": 1, "allow_generated": True,
                           "aliases": [], "episode_ids": ["E1"]}], "unmapped": []}  # fmt: skip
    existing = [cs.ExistingCategory(7, "dinner", "Dinner", "choosing dinner")]
    (cat,) = cs.validate_design(raw, recs, existing, {"dinner": "dinner"}).categories
    assert cat.existing_id == 7 and cat.display_name == "Dinner"  # 1 episode is fine here
    assert "dinner" in cat.aliases  # its own alias isn't a collision


def test_options_are_normalised_and_weighted() -> None:
    opts = [
        ConsideredOption(name="Yong Tau Foo", by="partner", stance="proposed"),
        ConsideredOption(name="Mala", by="partner", stance="rejected", reason="too heavy"),
    ]
    recs = _recs(
        _ep("dinner", "yong tau foo!", opts=opts), _ep("dinner", "Ramen"), _ep("dinner", "YTF")
    )
    design = cs.Design(
        [cs.CategoryProposal("dinner", "Dinner", "", 3, 1, True, ["dinner"],
                             [r.id for r in recs.values()])], [])  # fmt: skip
    seen = [
        (OptionSeen(category_phrase="Dinner", name="ramen", tags=["Japanese"], sentiment=0.8), T0)
    ]
    out = cs.build_options(
        design, list(recs.values()), seen, ["jack", "partner"], {"dinner": ["Ramen"]}, TZ
    )
    by = {o.name: o for o in out}
    assert set(by) == {"Yong Tau Foo", "Mala", "Ramen", "YTF"}
    ytf = by["Yong Tau Foo"]
    assert ytf.scores == [0.3, 1.0]  # proposed by partner, then chosen for both
    assert ytf.prefs() == {"partner": 1.26, "jack": 1.4}
    mala = by["Mala"]
    assert mala.base_weight == 0.6 and mala.prefs() == {"partner": 0.68}
    assert mala.evidence[0]["quote"] == "too heavy"
    assert by["Ramen"].existing and by["Ramen"].tags == ["japanese"]
    assert cs.canonical_choice(out, "dinner", "yong tau foo") == "Yong Tau Foo"


def test_low_confidence_facts_survive_only_when_repeated() -> None:
    def f(i: int, owner: str, text: str, conf: float) -> cs.FactRec:
        return cs.FactRec(f"f{i}", Fact(owner=owner, statement=text, confidence=conf), T0)

    facts = [
        f(1, "jack", "Jack hates coriander", 0.5),
        f(2, "jack", "Jack really hates coriander", 0.55),
        f(3, "jack", "Jack might like jazz", 0.4),
        f(4, "partner", "Sam is allergic to peanuts", 0.95),
        f(5, "mum", "Mum likes durian", 0.9),
    ]
    kept = [x.id for x in cs.filter_facts(facts, ["jack", "partner", "shared"])]
    assert kept == ["f1", "f2", "f4"]


def test_validate_notes_paths_and_evidence() -> None:
    fact = Fact(owner="partner", statement="peanut allergy", quote="cannot eat peanuts",
                confidence=0.9)  # fmt: skip
    facts = {"f1": cs.FactRec("f1", fact, T0)}
    raw = {"notes": [
        {"target": "profile", "topic": "", "lines": [
            {"text": "Allergic to peanuts.", "fact_ids": ["f1", "f9"], "confidence": 0.95}]},
        {"target": "topic", "topic": "Food", "lines": [
            {"text": "Likes light dinners", "fact_ids": [], "confidence": 0.7},
            {"text": "  ", "fact_ids": [], "confidence": 1}]},
        {"target": "topic", "topic": "food", "lines": [
            {"text": "Prefers YTF", "fact_ids": [], "confidence": 0.5}]},
    ]}  # fmt: skip
    notes = cs.validate_notes(raw, "partner", facts, TZ)
    assert [(n.path, len(n.lines)) for n in notes] == [
        ("people/partner.md", 1),
        ("memories/partner/food.md", 2),
    ]
    assert notes[0].lines[0]["evidence"] == [{"date": "2026-07-03", "quote": "cannot eat peanuts"}]
    assert notes[1].confidence == 0.6
    assert cs.note_path("shared", "topic", "Weekend plans") == (
        "shared/topics/weekend-plans.md",
        "Weekend plans",
    )

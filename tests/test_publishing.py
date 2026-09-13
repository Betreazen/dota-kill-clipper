import random
from datetime import datetime, timedelta

import pytest

from killclipper.publishing import (Schedule, action_text, fit_tags, kill_category, load_dictionary, metadata,
                                    review, tag_cost)

CHANNEL = "Betreazen highlights Dota 2"
CREDIT = '"Hot Pursuit" Kevin MacLeod (incompetech.com)'


def small_dictionary():
    lang = lambda word: {  # noqa: E731
        "hooks": {c: [f"{word} {c} hook"] for c in ("single", "double", "triple", "ultra", "rampage", "assist")},
        "intros": [f"{word} intro {{action}} KDA {{kda}}."],
        "paragraphs": [f"{word} paragraph about Techies on {{channel}} in {{year}}. " * 5],
        "cta": [f"{word} subscribe to {{channel}}!"],
        "keywords": [f"{word} keyword {i}" for i in range(50)],
        "hashtags": ["Dota2", "Techies", "Shorts"] + [f"{word}Tag{i}" for i in range(40)],
    }
    return {"ru": lang("ру"), "en": lang("en"), "tags": [f"tag number {i}" for i in range(80)]}


class FixedRng(random.Random):
    def __init__(self, language):
        super().__init__(1)
        self.language = language

    def choice(self, seq):
        return self.language if tuple(seq) == ("ru", "en") else super().choice(seq)


def test_kill_category_and_action_text_plurals():
    assert [kill_category(k) for k in (0, 1, 2, 3, 4, 5, 9)] == \
        ["assist", "single", "double", "triple", "ultra", "rampage", "rampage"]
    assert action_text(1, 0, "ru") == "1 убийство"
    assert action_text(2, 1, "ru") == "2 убийства + 1 ассист"
    assert action_text(5, 3, "ru") == "5 убийств + 3 ассиста"
    assert action_text(21, 11, "ru") == "21 убийство + 11 ассистов"
    assert action_text(0, 3, "ru") == "3 ассиста"
    assert action_text(1, 0, "en") == "1 kill" and action_text(2, 1, "en") == "2 kills + 1 assist"
    assert action_text(0, 3, "en") == "3 assists"


def test_title_uses_category_hook_and_required_postfix():
    for language, word in (("ru", "ру"), ("en", "en")):
        item = metadata(2, 1, (7, 2, 1), CHANNEL, CREDIT, now=datetime(2026, 9, 13),
                        rng=FixedRng(language), dictionary=small_dictionary())
        assert item["language"] == language
        assert item["title"] == f"{word} double hook | Dota 2 2026 | {CHANNEL}"


def test_description_single_language_credit_hashtags_and_limits():
    item = metadata(0, 2, (3, 1, 2), CHANNEL, CREDIT, now=datetime(2026, 9, 13),
                    rng=FixedRng("ru"), dictionary=small_dictionary())
    text = item["description"]
    assert text.startswith("ру assist hook") and "ру intro 2 ассиста KDA 3-1-2." in text
    assert " en " not in text and "{" not in text and "<" not in text
    assert CREDIT in text and len(text.encode("utf-8")) <= 5000
    hashtag_line = [line for line in text.splitlines() if line.startswith("#")][0]
    assert hashtag_line.split()[:3] == ["#Dota2", "#Techies", "#Shorts"] and len(hashtag_line.split()) <= 15


def test_tag_cost_counts_commas_and_quotes_for_spaces():
    assert tag_cost(["a b", "c"]) == 7
    candidates = ["dota", "Dota"] + [f"long tag {i}" for i in range(100)]
    tags = fit_tags(candidates)
    assert tag_cost(tags) <= 500
    assert all(tag_cost(tags + [t]) > 500 for t in candidates if t.lower() not in {x.lower() for x in tags})
    assert "dota" in tags and "Dota" not in tags  # case-insensitive dedup


def test_real_dictionary_fills_limits_in_both_languages():
    dictionary = load_dictionary()
    for seed in range(20):
        for language in ("ru", "en"):
            rng = FixedRng(language)
            rng.seed(seed)
            item = metadata(seed % 6, seed % 3, (seed, 1, 2), CHANNEL, CREDIT, now=datetime(2026, 9, 13),
                            rng=rng, dictionary=dictionary)
            size = len(item["description"].encode("utf-8"))
            assert len(item["title"]) <= 100 and item["title"].endswith(f"| Dota 2 2026 | {CHANNEL}")
            assert 3500 <= size <= 5000, (language, size)
            assert CREDIT in item["description"] and "{" not in item["description"]
            assert 440 <= tag_cost(item["tags"]) <= 500
            assert "dota 2 2026" in item["tags"]


def test_text_never_names_a_different_kill_streak():
    dictionary = load_dictionary()
    other = {1: ("трипл", "triple", "рампаг", "rampage", "дабл", "double"), 2: ("трипл", "triple", "рампаг", "rampage")}
    for kills, banned in other.items():
        for seed in range(15):
            for language in ("ru", "en"):
                rng = FixedRng(language)
                rng.seed(seed)
                text = metadata(kills, 0, (kills, 0, 0), CHANNEL, CREDIT, rng=rng, dictionary=dictionary)
                body = (text["title"] + text["description"]).lower()
                assert not [w for w in banned if w in body], (kills, language, seed)


def test_schedule_falls_back_to_fixed_minsk_offset_without_tzdata(tmp_path, monkeypatch):
    import killclipper.publishing as publishing

    def missing(name):
        raise publishing.ZoneInfoNotFoundError(name)

    monkeypatch.setattr(publishing, "ZoneInfo", missing)
    plan = Schedule(tmp_path / "s.json")
    slot = plan.reserve(datetime(2026, 9, 13, 9, 0, tzinfo=plan.zone))
    assert slot.isoformat() == "2026-09-13T10:00:00+03:00"


def test_schedule_skips_near_and_used_slots_and_persists(tmp_path):
    plan = Schedule(tmp_path / "schedule.json")
    zone = plan.zone
    assert plan.reserve(datetime(2026, 9, 13, 9, 50, tzinfo=zone)).hour == 12  # 10:00 is under 15 min away
    assert plan.reserve(datetime(2026, 9, 13, 9, 0, tzinfo=zone)).hour == 10
    late = Schedule(tmp_path / "schedule.json").reserve(datetime(2026, 9, 13, 21, 50, tzinfo=zone))
    assert (late.day, late.hour) == (14, 8)
    again = Schedule(tmp_path / "schedule.json")
    again.release(late)
    assert Schedule(tmp_path / "schedule.json").reserve(datetime(2026, 9, 13, 21, 50, tzinfo=zone)) == late


def test_schedule_fills_whole_day_then_next_day(tmp_path):
    plan = Schedule(tmp_path / "s.json")
    now = datetime(2026, 9, 13, 7, 0, tzinfo=plan.zone)
    slots = [plan.reserve(now) for _ in range(9)]
    assert [s.hour for s in slots] == [8, 10, 12, 14, 16, 18, 20, 22, 8]
    assert slots[-1] - slots[0] == timedelta(days=1)


def test_review_half_median_rule_resists_viral_outlier():
    views = {f"v{i}": v for i, v in enumerate([40, 45, 50, 55, 60, 12, 19, 100_000])}
    report = review(views)
    assert report["median"] == 47.5 and report["threshold"] == 23.75
    assert set(report["hide"]) == {"v5", "v6"}
    assert report["max"] == ["v7", 100_000] and report["min"] == ["v5", 12]


def test_review_needs_enough_videos():
    report = review({"a": 1, "b": 100, "c": 100})
    assert report["hide"] == [] and report["videos"] == 3
    assert review({})["max"] is None

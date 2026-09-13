"""YouTube metadata from bilingual dictionaries, publication slots and the monthly view review."""
import json
import os
import random
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .locking import FileLock

DICTIONARY = Path(__file__).with_name("seo_dictionary.json")
SLOT_HOURS = tuple(range(8, 23, 2))  # 08:00 ... 22:00, every two hours
SLOT_MARGIN = timedelta(minutes=15)  # the upload must finish before publishAt
TITLE_LIMIT, DESCRIPTION_BYTES, TAGS_LIMIT = 100, 5000, 500
KEYWORD_RESERVE = 600                # bytes kept for the keyword line when adding paragraphs
FIRST_HASHTAGS = ["Dota2", "Techies", "Shorts"]  # YouTube shows the first three above the title
HASHTAG_COUNT = 15                   # over 60 hashtags makes YouTube ignore all of them
MIN_REVIEW_VIDEOS = 8
KEYWORDS_LABEL = {"ru": "Ключевые слова: ", "en": "Keywords: "}
# text naming another kill streak than the clip has is left out (no "#TripleKill" on a double kill)
STREAK_WORDS = {"double": ("дабл", "double"), "triple": ("трипл", "triple"),
                "ultra": ("ультра", "ultra"), "rampage": ("рампаг", "rampage")}


def zone(name: str):
    """IANA zone; Windows Python without the tzdata package falls back to the fixed Minsk offset."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=3))  # ponytail: Europe/Minsk has had no DST since 2011


@lru_cache(maxsize=1)
def load_dictionary(path: str = str(DICTIONARY)) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def kill_category(kills: int) -> str:
    return ("assist", "single", "double", "triple", "ultra")[kills] if kills < 5 else "rampage"


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def action_text(kills: int, assists: int, language: str) -> str:
    parts = []
    if language == "ru":
        if kills:
            parts.append(f"{kills} {_ru_plural(kills, 'убийство', 'убийства', 'убийств')}")
        if assists:
            parts.append(f"{assists} {_ru_plural(assists, 'ассист', 'ассиста', 'ассистов')}")
    else:
        if kills:
            parts.append(f"{kills} kill{'s' if kills != 1 else ''}")
        if assists:
            parts.append(f"{assists} assist{'s' if assists != 1 else ''}")
    return " + ".join(parts)


def fits_category(text: str, category: str) -> bool:
    low = text.lower()
    return not any(word in low for name, words in STREAK_WORDS.items() if name != category for word in words)


def tag_cost(tags: list[str]) -> int:
    """YouTube counts commas, and quotes around tags that contain spaces."""
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(len(tags) - 1, 0)


def fit_tags(candidates: list[str], limit: int = TAGS_LIMIT) -> list[str]:
    tags, seen = [], set()
    for tag in candidates:
        if tag.lower() in seen or tag_cost(tags + [tag]) > limit:
            continue
        tags.append(tag)
        seen.add(tag.lower())
    return tags


def _clean(text: str) -> str:
    return text.replace("<", "").replace(">", "")


def metadata(kills: int, assists: int, kda, channel: str, credit: str = "", now: datetime | None = None,
             rng=random, dictionary: dict | None = None) -> dict:
    """Title, description and tags in one randomly chosen language (tags are mixed)."""
    d = dictionary or load_dictionary()
    now = now or datetime.now()
    language = rng.choice(("ru", "en"))
    category = kill_category(kills)
    words = {key: [x for x in value if fits_category(x, category)] if isinstance(value, list) else value
             for key, value in d[language].items()}
    fill = {"action": action_text(kills, assists, language), "kda": "-".join(map(str, kda)),
            "year": now.year, "channel": channel}
    postfix = f" | Dota 2 {now.year} | {channel}"
    hooks = [h for h in words["hooks"][category] if len(h) + len(postfix) <= TITLE_LIMIT]
    hook = rng.choice(hooks) if hooks else "Techies"
    title = _clean(hook + postfix)[:TITLE_LIMIT]

    head = [hook, rng.choice(words["intros"]).format(**fill)]
    firsts = {h.lower() for h in FIRST_HASHTAGS}
    pool = [h for h in words["hashtags"] if h.lower() not in firsts]
    hashtags = FIRST_HASHTAGS + rng.sample(pool, min(HASHTAG_COUNT - len(FIRST_HASHTAGS), len(pool)))
    tail = [rng.choice(words["cta"]).format(**fill)]
    closing = [" ".join("#" + h for h in hashtags), credit]

    def build(paragraphs: list[str], keywords: list[str]) -> str:
        keyword_line = KEYWORDS_LABEL[language] + ", ".join(keywords) if keywords else ""
        parts = [*head, *paragraphs, *tail, keyword_line, *closing]
        return _clean("\n\n".join(p for p in parts if p))

    paragraphs = []
    for p in rng.sample(words["paragraphs"], len(words["paragraphs"])):
        if len(build(paragraphs + [p.format(**fill)], []).encode()) <= DESCRIPTION_BYTES - KEYWORD_RESERVE:
            paragraphs.append(p.format(**fill))
    keywords = []
    for k in rng.sample(words["keywords"], len(words["keywords"])):
        if len(build(paragraphs, keywords + [k]).encode()) <= DESCRIPTION_BYTES:
            keywords.append(k)

    year_tags = [f"dota 2 {now.year}", f"дота 2 {now.year}", f"techies {now.year}"]
    tags = fit_tags(d["tags"][:5] + year_tags + d["tags"][5:])
    return {"title": title, "description": build(paragraphs, keywords), "tags": tags, "language": language}


PLAYLIST_ALL = "Dota 2"


def playlist_title(kills: int | None) -> str:
    return PLAYLIST_ALL if not kills else f"{kills} kill video"


def playlist_description(kills: int | None, channel: str, dictionary: dict | None = None) -> str:
    """SEO description of 'Dota 2' (kills=None) or 'N kill video': RU and EN texts, keywords, hashtags, up to 5000 bytes.

    Deterministic per playlist, so re-syncing does not reshuffle the text.
    """
    d = dictionary or load_dictionary()
    category = "all" if not kills else kill_category(kills)
    title = playlist_title(kills)
    rng = random.Random(title)
    keep = (lambda text: True) if not kills else (lambda text: fits_category(text, category))
    paragraphs = [p.format(title=title, channel=channel, count=action_text(kills or 0, 0, lang))
                  for lang in ("ru", "en") for p in d["playlists"][category][lang]]
    keywords = {lang: [k for k in d[lang]["keywords"] if keep(k)] for lang in ("ru", "en")}
    for words in keywords.values():
        rng.shuffle(words)
    firsts = {h.lower() for h in FIRST_HASHTAGS}
    pool = sorted({h for lang in ("ru", "en") for h in d[lang]["hashtags"] if keep(h) and h.lower() not in firsts})
    hashtags = " ".join("#" + h for h in FIRST_HASHTAGS + rng.sample(pool, min(HASHTAG_COUNT - len(FIRST_HASHTAGS), len(pool))))
    chosen: dict[str, list[str]] = {"ru": [], "en": []}

    def build() -> str:
        lines = [*paragraphs] + [KEYWORDS_LABEL[lang] + ", ".join(chosen[lang]) for lang in ("ru", "en") if chosen[lang]]
        return _clean("\n\n".join(lines + [hashtags]))

    for i in range(max(len(w) for w in keywords.values())):
        for lang in ("ru", "en"):  # alternate so both languages get keywords
            if i < len(keywords[lang]):
                chosen[lang].append(keywords[lang][i])
                if len(build().encode()) > DESCRIPTION_BYTES:
                    chosen[lang].pop()
    return build()


def write_atomic(path: Path, text: str) -> None:
    """Temp file + replace: a crash mid-write never leaves a truncated JSON behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Schedule:
    """Persistent allocator of publication slots (08:00-22:00 every 2 h, local zone)."""

    def __init__(self, path: Path, tz_name: str = "Europe/Minsk"):
        self.path, self.zone = Path(path), zone(tz_name)
        self.used = self._read()

    def _read(self) -> set[str]:
        try:
            return set(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return set()

    def reserve(self, now: datetime | None = None, margin: timedelta = SLOT_MARGIN) -> datetime:
        with FileLock(self.path.with_suffix('.lock')):
            return self._reserve(now, margin)

    def _reserve(self, now, margin):
        now = (now or datetime.now(self.zone)).astimezone(self.zone)
        self.used = self._read()
        day = now.date()
        while True:
            for hour in SLOT_HOURS:
                slot = datetime(day.year, day.month, day.day, hour, tzinfo=self.zone)
                if slot >= now + margin and slot.isoformat() not in self.used:
                    self.used.add(slot.isoformat())
                    self._save(now)
                    return slot
            day += timedelta(days=1)

    def release(self, slot: datetime) -> None:
        with FileLock(self.path.with_suffix('.lock')):
            self.used = self._read()
            self.used.discard(slot.astimezone(self.zone).isoformat())
            self._save(datetime.now(self.zone))

    def _save(self, now: datetime) -> None:
        # past slots are dropped so the file stays small
        keep = sorted(s for s in self.used if datetime.fromisoformat(s) > now - timedelta(days=2))
        write_atomic(self.path, json.dumps(keep))


def review(counts: dict[str, int], min_videos: int = MIN_REVIEW_VIDEOS) -> dict:
    """Monthly slice: hide videos with fewer views than half the median.

    The median ignores how far a viral video shoots up, so one hit cannot make the rest look weak.
    Nothing is hidden until at least `min_videos` videos are in the slice.
    """
    ranked = sorted(counts.items(), key=lambda kv: kv[1])
    report = {"videos": len(ranked), "median": None, "threshold": None,
              "max": list(ranked[-1]) if ranked else None, "min": list(ranked[0]) if ranked else None,
              "hide": [], "counts": dict(counts)}
    if ranked:
        mid = median(v for _, v in ranked)
        report.update(median=mid, threshold=mid / 2)
        if len(ranked) >= min_videos:
            report["hide"] = [vid for vid, views in ranked if views < mid / 2]
    return report

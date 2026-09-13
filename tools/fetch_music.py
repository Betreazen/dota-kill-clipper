"""Download an energetic subset of Kevin MacLeod's CC BY 4.0 library from incompetech.com.

Usage: python tools/fetch_music.py [target_dir] [count]
Writes credits.json (file name -> track title) next to the tracks; the renderer uses it
for the attribution line required by CC BY 4.0. Existing files are skipped.
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

CATALOG = "https://incompetech.com/music/royalty-free/pieces.json"
MP3_URL = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/"
WANTED = {"Action", "Driving", "Intense", "Epic", "Aggressive", "Humorous", "Bouncy"}
UNWANTED = {"Somber", "Calming", "Relaxed", "Calm", "Eerie", "Unnerving"}


def seconds(length: str | None) -> int:
    parts = [int(p) for p in (length or "0").split(":") if p.isdigit()]
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def pick(pieces: list[dict], count: int) -> list[dict]:
    """Tracks with the most wanted feels first; 1-5 minutes long; no calm/eerie moods."""
    scored = []
    for p in pieces:
        feels = {f.strip() for f in (p.get("feel") or "").split(",")}
        score = len(feels & WANTED)
        if score and not feels & UNWANTED and 60 <= seconds(p.get("length")) <= 300 and p.get("filename"):
            scored.append((-score, p["title"], p))
    return [p for _, _, p in sorted(scored, key=lambda x: x[:2])[:count]]


def main(target: str = r"C:\Highlights\music", count: int = 150) -> None:
    folder = Path(target)
    folder.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(CATALOG, timeout=60) as r:
        pieces = json.load(r)
    credits_file = folder / "credits.json"
    credits = json.loads(credits_file.read_text(encoding="utf-8")) if credits_file.exists() else {}
    chosen = pick(pieces, count)
    for i, p in enumerate(chosen, 1):
        name = p["filename"]
        credits[name] = p["title"]
        dst = folder / name
        if dst.exists():
            continue
        tmp = dst.with_suffix(".part")
        try:
            with urllib.request.urlopen(MP3_URL + urllib.parse.quote(name), timeout=120) as r:
                tmp.write_bytes(r.read())
            tmp.replace(dst)
            print(f"[{i}/{len(chosen)}] {name}")
        except OSError as e:
            tmp.unlink(missing_ok=True)
            credits.pop(name, None)
            print(f"[{i}/{len(chosen)}] FAILED {name}: {e}")
        time.sleep(0.5)  # be polite to incompetech.com
    credits_file.write_text(json.dumps(credits, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"done: {sum(1 for n in credits if (folder / n).exists())} tracks with credits in {folder}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"C:\Highlights\music",
         int(sys.argv[2]) if len(sys.argv) > 2 else 150)

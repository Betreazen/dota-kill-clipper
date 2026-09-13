"""YouTube Data API client pinned to one channel: login, scheduled upload, playlists, review, unlisting.

Used only from the pipeline worker thread (the Google HTTP client is not thread-safe).
CLI check outside OBS:
    python -m killclipper.youtube login  <client_secret.json>
    python -m killclipper.youtube review <client_secret.json>   (dry run: prints the report only)
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .publishing import action_text, review

SCOPES = ["https://www.googleapis.com/auth/youtube", "https://www.googleapis.com/auth/youtube.upload"]
CHANNEL_HANDLE = "@Betreazen_highlights"
CATEGORY_GAMING = "20"
REVIEW_MIN_AGE = timedelta(days=30)
RECHECK_AFTER = 60.0  # seconds between silent reconnect attempts


class NotLoggedIn(RuntimeError):
    pass


def playlists_for(kills: int) -> list[tuple[str, str]]:
    """'Dota 2' gets every video; 'N kill video' gets clips with N kills (assists allowed)."""
    items = [("Dota 2", "Все хайлайты Dota 2 на Techies: убийства, ассисты, мины и взрывы. "
                        "All Dota 2 Techies highlights: kills, assists, mines and explosions.")]
    if kills:
        items.append((f"{kills} kill video",
                      f"Dota 2 Techies: клипы, где {action_text(kills, 0, 'ru')} (с ассистами тоже). "
                      f"Dota 2 Techies clips with {action_text(kills, 0, 'en')}, assists included."))
    return items


class YoutubeClient:
    def __init__(self, credentials_file: str, token_file: str, handle: str = CHANNEL_HANDLE):
        self.credentials_file, self.token_file, self.handle = credentials_file, Path(token_file), handle
        self.service = None
        self.channel_id = None
        self.channel_title = ""
        self._failed_at = 0.0

    def authorized(self) -> bool:
        if self.service:
            return True
        if not self.token_file.exists() or time.time() - self._failed_at < RECHECK_AFTER:
            return False
        try:
            self.connect(interactive=False)
            return True
        except Exception:
            self._failed_at = time.time()
            return False

    def connect(self, interactive: bool = False) -> "YoutubeClient":
        """Load/refresh the saved token (or open the browser when interactive) and verify the channel."""
        if not interactive and not self.token_file.exists():
            raise NotLoggedIn("YouTube: not logged in")
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = None
        if self.token_file.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
            if not creds.valid and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self._save(creds)
        if not creds or not creds.valid:
            if not interactive:
                raise NotLoggedIn("YouTube: token expired, log in again")
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
            creds = flow.run_local_server(port=0, prompt="select_account consent")
            self._save(creds)
        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        target = service.channels().list(part="id,snippet", forHandle=self.handle).execute().get("items", [])
        mine = service.channels().list(part="id,snippet", mine=True).execute().get("items", [])
        if len(target) != 1 or not mine or target[0]["id"] != mine[0]["id"]:
            self.token_file.unlink(missing_ok=True)  # wrong channel: force a fresh login next time
            got = mine[0]["snippet"]["title"] if mine else "none"
            raise RuntimeError(f"Logged in as channel '{got}', expected {self.handle}; log in again and pick it")
        self.service, self.channel_id, self.channel_title = service, target[0]["id"], target[0]["snippet"]["title"]
        return self

    def _save(self, creds) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(creds.to_json(), encoding="utf-8")

    def insert(self, path: str, meta: dict, publish_at: datetime) -> str:
        """Upload as private with a scheduled publish time. Returns the video id."""
        from googleapiclient.http import MediaFileUpload

        body = {
            "snippet": {"title": meta["title"], "description": meta["description"], "tags": meta["tags"],
                        "categoryId": CATEGORY_GAMING, "defaultLanguage": meta["language"],
                        "defaultAudioLanguage": meta["language"]},
            "status": {"privacyStatus": "private",
                       "publishAt": publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False},
        }
        media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=-1, resumable=True)
        return self.service.videos().insert(part="snippet,status", body=body, media_body=media).execute()["id"]

    def add_to_playlists(self, video_id: str, kills: int) -> None:
        """Create missing playlists and add the video; already added videos are skipped."""
        existing = self._playlist_ids()
        for title, description in playlists_for(kills):
            if title not in existing:
                created = self.service.playlists().insert(part="snippet,status", body={
                    "snippet": {"title": title, "description": description},
                    "status": {"privacyStatus": "public"}}).execute()
                existing[title] = created["id"]
            elif self.service.playlistItems().list(part="id", playlistId=existing[title],
                                                   videoId=video_id).execute().get("items"):
                continue  # retry after a partial failure: do not add the same video twice
            self.service.playlistItems().insert(part="snippet", body={"snippet": {
                "playlistId": existing[title], "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()

    def _playlist_ids(self) -> dict[str, str]:
        ids = {}
        request = self.service.playlists().list(part="snippet", mine=True, maxResults=50)
        while request:
            page = request.execute()
            ids.update({p["snippet"]["title"]: p["id"] for p in page.get("items", [])})
            request = self.service.playlists().list_next(request, page)
        return ids

    def review_counts(self, now: datetime | None = None) -> dict[str, int]:
        """View counts of public videos published at least 30 days ago."""
        now = now or datetime.now(timezone.utc)
        channel = self.service.channels().list(part="contentDetails", id=self.channel_id).execute()["items"][0]
        uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
        ids = []
        request = self.service.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50)
        while request:
            page = request.execute()
            ids += [item["contentDetails"]["videoId"] for item in page.get("items", [])]
            request = self.service.playlistItems().list_next(request, page)
        counts = {}
        for start in range(0, len(ids), 50):
            page = self.service.videos().list(part="snippet,status,statistics", id=",".join(ids[start:start + 50])).execute()
            for v in page.get("items", []):
                published = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
                if v["status"].get("privacyStatus") == "public" and now - published >= REVIEW_MIN_AGE:
                    counts[v["id"]] = int(v["statistics"].get("viewCount", 0))
        return counts

    def unlist(self, video_ids: list[str]) -> None:
        """Hide videos from the channel and search (unlisted). Never deletes anything."""
        for vid in video_ids:
            items = self.service.videos().list(part="status", id=vid).execute().get("items", [])
            if not items:
                continue
            status = {**items[0]["status"], "privacyStatus": "unlisted"}  # keep license/embeddable/etc.
            self.service.videos().update(part="status", body={"id": vid, "status": status}).execute()


if __name__ == "__main__":
    command, secret = sys.argv[1], sys.argv[2]
    data_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "dota-kill-clipper"
    yt = YoutubeClient(secret, str(data_dir / "youtube-token.json")).connect(interactive=command == "login")
    print(f"channel: {yt.channel_title} ({yt.channel_id})")
    if command == "review":
        print(json.dumps(review(yt.review_counts()), ensure_ascii=False, indent=1))

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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .publishing import playlist_description, playlist_title, review

SCOPES = ["https://www.googleapis.com/auth/youtube", "https://www.googleapis.com/auth/youtube.upload"]
CHANNEL_HANDLE = "@Betreazen_highlights"
CHANNEL_NAME = "Betreazen highlights Dota 2"
SYNC_KILL_PLAYLISTS = 5  # "1 kill video" ... "5 kill video" exist up front; 6+ are created on demand
CATEGORY_GAMING = "20"
REVIEW_MIN_AGE = timedelta(days=30)
RECHECK_AFTER = 60.0  # seconds between silent reconnect attempts
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
PLAYLIST_LIST_LAG = 3600  # seconds a freshly created playlist id is trusted without playlists.list
STATUS_WRITABLE = ("privacyStatus", "embeddable", "license", "publicStatsViewable", "selfDeclaredMadeForKids")


class NotLoggedIn(RuntimeError):
    pass


def _revoke(token: str) -> None:
    body = urllib.parse.urlencode({"token": token}).encode()
    request = urllib.request.Request(REVOKE_URL, data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    urllib.request.urlopen(request, timeout=15).close()


def playlists_for(kills: int) -> list[tuple[str, str]]:
    """'Dota 2' gets every video; 'N kill video' gets clips with N kills (assists allowed)."""
    counts = [None, kills] if kills else [None]
    return [(playlist_title(k), playlist_description(k, CHANNEL_NAME)) for k in counts]


class YoutubeClient:
    def __init__(self, credentials_file: str, token_file: str, handle: str = CHANNEL_HANDLE,
                 playlist_cache: str | None = None):
        self.credentials_file, self.token_file, self.handle = credentials_file, Path(token_file), handle
        self.playlist_cache = Path(playlist_cache) if playlist_cache else None
        self._created: dict[str, list] = {}  # title -> [playlist id, created at]
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
        # the login button must always open the browser, even with a valid token (switch account/channel, re-consent)
        if not interactive and self.token_file.exists():
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

    def logout(self, revoke=None) -> None:
        """Forget the saved token, then revoke the grant at Google (raises if Google is unreachable)."""
        try:
            saved = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self.service, self.channel_id, self.channel_title = None, None, ""
        self.token_file.unlink(missing_ok=True)
        token = saved.get("refresh_token") or saved.get("token")
        if token:
            (revoke or _revoke)(token)

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
                self._remember_playlist(title, created["id"])
            elif self.service.playlistItems().list(part="id", playlistId=existing[title],
                                                   videoId=video_id).execute().get("items"):
                continue  # retry after a partial failure: do not add the same video twice
            self.service.playlistItems().insert(part="snippet", body={"snippet": {
                "playlistId": existing[title], "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()

    def sync_playlists(self, max_kills: int = SYNC_KILL_PLAYLISTS) -> dict[str, str]:
        """Create missing playlists and rewrite descriptions of existing ones. Returns title -> action."""
        existing = self._playlist_ids()
        result = {}
        for kills in [None, *range(1, max_kills + 1)]:
            snippet = {"title": playlist_title(kills), "description": playlist_description(kills, CHANNEL_NAME)}
            if snippet["title"] in existing:  # update needs the title; an omitted description would be wiped
                self.service.playlists().update(part="snippet", body={"id": existing[snippet["title"]], "snippet": snippet}).execute()
                result[snippet["title"]] = "updated"
            else:
                created = self.service.playlists().insert(part="snippet,status", body={
                    "snippet": snippet, "status": {"privacyStatus": "public"}}).execute()
                existing[snippet["title"]] = created["id"]
                self._remember_playlist(snippet["title"], created["id"])
                result[snippet["title"]] = "created"
        return result

    def _recent_playlists(self) -> dict[str, list]:
        data = dict(self._created)
        if self.playlist_cache:
            try:
                data.update(json.loads(self.playlist_cache.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return {title: v for title, v in data.items() if time.time() - v[1] < PLAYLIST_LIST_LAG}

    def _remember_playlist(self, title: str, playlist_id: str) -> None:
        self._created[title] = [playlist_id, time.time()]
        if self.playlist_cache:
            data = {**self._recent_playlists(), title: self._created[title]}
            self.playlist_cache.parent.mkdir(parents=True, exist_ok=True)
            self.playlist_cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _playlist_ids(self) -> dict[str, str]:
        # playlists.list can miss a playlist for a while after it is created: fall back to what we created recently
        ids = {title: v[0] for title, v in self._recent_playlists().items()}
        request = self.service.playlists().list(part="snippet", mine=True, maxResults=50)
        while request:
            page = request.execute()
            ids.update({p["snippet"]["title"]: p["id"] for p in page.get("items", [])})
            request = self.service.playlists().list_next(request, page)
        return ids

    def find_upload(self, title: str, publish_at: str, lookback: int = 50) -> str | None:
        """Id of a recent upload with this title and scheduled time (checks an upload whose answer was lost)."""
        target = datetime.fromisoformat(publish_at).astimezone(timezone.utc)
        channel = self.service.channels().list(part="contentDetails", id=self.channel_id).execute()["items"][0]
        uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
        items = self.service.playlistItems().list(part="contentDetails", playlistId=uploads,
                                                  maxResults=lookback).execute().get("items", [])
        ids = [item["contentDetails"]["videoId"] for item in items]
        if not ids:
            return None
        for v in self.service.videos().list(part="snippet,status", id=",".join(ids)).execute().get("items", []):
            when = v["status"].get("publishAt") or v["snippet"].get("publishedAt")
            if v["snippet"].get("title") == title and when and \
                    abs(datetime.fromisoformat(when.replace("Z", "+00:00")) - target) <= timedelta(minutes=5):
                return v["id"]
        return None

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
            # only writable fields: with read-only ones (uploadStatus, madeForKids...) YouTube silently ignores the change
            current = items[0]["status"]
            status = {"privacyStatus": "unlisted",
                      **{k: current[k] for k in STATUS_WRITABLE if k in current and k != "privacyStatus"}}
            self.service.videos().update(part="status", body={"id": vid, "status": status}).execute()


if __name__ == "__main__":
    command, secret = sys.argv[1], sys.argv[2]
    data_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "dota-kill-clipper"
    yt = YoutubeClient(secret, str(data_dir / "youtube-token.json"),
                       playlist_cache=str(data_dir / "playlists.json")).connect(interactive=command == "login")
    print(f"channel: {yt.channel_title} ({yt.channel_id})")
    if command == "review":
        print(json.dumps(review(yt.review_counts()), ensure_ascii=False, indent=1))
    if command == "playlists":  # create missing playlists and refresh their SEO descriptions
        print(json.dumps(yt.sync_playlists(), ensure_ascii=False, indent=1))

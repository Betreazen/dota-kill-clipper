from datetime import datetime, timedelta, timezone

import pytest

from killclipper.youtube import NotLoggedIn, YoutubeClient, playlists_for


class Call:
    def __init__(self, service, resource, method, kwargs):
        self.service, self.resource, self.method, self.kwargs = service, resource, method, kwargs

    def execute(self):
        return self.service.respond(self.resource, self.method, self.kwargs)


class Resource:
    def __init__(self, service, name):
        self.service, self.name = service, name

    def __getattr__(self, method):
        if method == "list_next":
            return lambda request, page: None
        return lambda **kw: self.service.record(self.name, method, kw)


class FakeService:
    def __init__(self, playlists=(), videos=()):
        self.calls, self.playlist_ids, self.video_items, self.added = [], dict(playlists), list(videos), set()

    def record(self, resource, method, kwargs):
        self.calls.append((resource, method, kwargs))
        return Call(self, resource, method, kwargs)

    def __getattr__(self, name):
        return lambda: Resource(self, name)

    def respond(self, resource, method, kw):
        if (resource, method) == ("videos", "insert"):
            return {"id": "new-video"}
        if (resource, method) == ("playlists", "list"):
            return {"items": [{"id": pid, "snippet": {"title": t}} for t, pid in self.playlist_ids.items()]}
        if (resource, method) == ("playlists", "insert"):
            pid = f"pl-{len(self.playlist_ids)}"
            self.playlist_ids[kw["body"]["snippet"]["title"]] = pid
            return {"id": pid}
        if (resource, method) == ("channels", "list"):
            return {"items": [{"id": "UC1", "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}
        if (resource, method) == ("playlistItems", "list") and "videoId" in kw:
            return {"items": [{"id": "item"}] if (kw["playlistId"], kw["videoId"]) in self.added else []}
        if (resource, method) == ("playlistItems", "insert"):
            snippet = kw["body"]["snippet"]
            self.added.add((snippet["playlistId"], snippet["resourceId"]["videoId"]))
            return {}
        if (resource, method) == ("playlistItems", "list"):
            return {"items": [{"contentDetails": {"videoId": v["id"]}} for v in self.video_items]}
        if (resource, method) == ("videos", "list"):
            ids = kw["id"].split(",")
            return {"items": [v for v in self.video_items if v["id"] in ids]}
        return {}


def client(service):
    c = YoutubeClient("secret.json", "token.json")
    c.service, c.channel_id = service, "UC1"
    return c


META = {"title": "T", "description": "D", "tags": ["dota 2"], "language": "ru"}


def test_playlists_by_kill_count():
    assert [t for t, _ in playlists_for(0)] == ["Dota 2"]
    assert [t for t, _ in playlists_for(1)] == ["Dota 2", "1 kill video"]
    assert [t for t, _ in playlists_for(3)] == ["Dota 2", "3 kill video"]
    assert all(desc for _, desc in playlists_for(2))


def test_upload_is_private_scheduled_and_added_to_playlists(tmp_path):
    short = tmp_path / "short.mp4"
    short.write_bytes(b"video")
    service = FakeService(playlists={"Dota 2": "pl-dota"})
    slot = datetime(2026, 9, 14, 8, 0, tzinfo=timezone(timedelta(hours=3)))
    c = client(service)
    assert c.insert(str(short), META, publish_at=slot) == "new-video"
    c.add_to_playlists("new-video", kills=2)
    insert = [kw for r, m, kw in service.calls if (r, m) == ("videos", "insert")][0]
    snippet, status = insert["body"]["snippet"], insert["body"]["status"]
    assert snippet["categoryId"] == "20" and snippet["defaultLanguage"] == "ru" and snippet["title"] == "T"
    assert status["privacyStatus"] == "private" and status["publishAt"] == "2026-09-14T05:00:00Z"
    assert status["selfDeclaredMadeForKids"] is False
    created = [kw["body"]["snippet"]["title"] for r, m, kw in service.calls if (r, m) == ("playlists", "insert")]
    assert created == ["2 kill video"]
    added = [kw["body"]["snippet"]["playlistId"] for r, m, kw in service.calls if (r, m) == ("playlistItems", "insert")]
    assert added == ["pl-dota", "pl-1"]


def video(vid, views, days_old, privacy="public"):
    published = (datetime(2026, 9, 1, tzinfo=timezone.utc) - timedelta(days=days_old)).isoformat().replace("+00:00", "Z")
    return {"id": vid, "snippet": {"publishedAt": published, "title": vid, "categoryId": "20"},
            "status": {"privacyStatus": privacy}, "statistics": {"viewCount": str(views)}}


def test_review_counts_only_public_videos_older_than_30_days():
    service = FakeService(videos=[video("old", 5, 40), video("new", 1, 3), video("hidden", 2, 90, "unlisted")])
    counts = client(service).review_counts(now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert counts == {"old": 5}


def test_unlist_changes_privacy_and_never_deletes():
    service = FakeService(videos=[video("weak", 1, 40)])
    client(service).unlist(["weak"])
    update = [kw for r, m, kw in service.calls if (r, m) == ("videos", "update")][0]
    assert update["body"]["status"]["privacyStatus"] == "unlisted" and update["body"]["id"] == "weak"
    assert not [c for c in service.calls if c[1] == "delete"]


def test_connect_without_token_is_not_interactive(tmp_path):
    c = YoutubeClient(str(tmp_path / "secret.json"), str(tmp_path / "token.json"))
    with pytest.raises(NotLoggedIn):
        c.connect(interactive=False)
    assert c.authorized() is False

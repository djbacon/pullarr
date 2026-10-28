#!/usr/bin/env python3
from __future__ import annotations

import logging
import json
import threading
import tempfile
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import plex
import channel_assets
import ytdlp_runtime
import video_encoding


VERSION = "0.16.1"
USER_AGENT = f"pullarr/{VERSION}"
ATOM_NS = "http://www.w3.org/2005/Atom"
YT_NS = "http://www.youtube.com/xml/schemas/2015"
DOWNLOAD_MODES = ("all", "latest", "future")
STOP = False


@dataclass(frozen=True)
class VideoEntry:
    video_id: str
    channel_id: str
    channel_title: str
    title: str
    published: str
    url: str
    duration: int | None = None

    @property
    def release_date(self) -> str:
        try:
            dt = datetime.fromisoformat(self.published.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            return self.published[:10] if len(self.published) >= 10 else "unknown-date"


class DownloadProgress:
    """Transient progress for one pipeline stage; durable queue state stays in SQLite."""
    def __init__(self):
        self.lock = threading.Lock()
        self.active = None

    def start(self, entry: VideoEntry, status: str = "downloading") -> None:
        with self.lock:
            self.active = {"video_id": entry.video_id, "channel_id": entry.channel_id,
                           "channel_title": entry.channel_title, "title": entry.title,
                           "status": status, "percent": None if status == "converting" else 0.0}

    def update(self, percent: float | None) -> None:
        with self.lock:
            if self.active is not None:
                self.active["percent"] = None if percent is None else round(max(0.0, min(100.0, percent)), 1)

    def clear(self) -> None:
        with self.lock:
            self.active = None

    def snapshot(self) -> dict | None:
        with self.lock:
            return dict(self.active) if self.active is not None else None


DOWNLOAD_PROGRESS = DownloadProgress()
CONVERSION_PROGRESS = DownloadProgress()


def active_downloads() -> list[dict]:
    items = []
    for progress in (CONVERSION_PROGRESS, DOWNLOAD_PROGRESS):
        item = progress.snapshot()
        if item and not any(row["video_id"] == item["video_id"] for row in items):
            items.append(item)
    return items


@dataclass(frozen=True)
class ChannelConfig:
    channel_id: str
    enabled: bool = True


@dataclass(frozen=True)
class AppConfig:
    poll_interval_seconds: int = 900
    download_root: Path = Path("/downloads")
    database_path: Path = Path("/config/state.db")
    video_format: str = "bestvideo*+bestaudio/best"
    merge_output_format: str = "mkv"


def handle_signal(signum, _frame):
    global STOP
    logging.info("Received signal %s; shutting down", signum)
    STOP = True
    WAKE.set()


WAKE = threading.Event()
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")


def extract_channel_id(page: str) -> str | None:
    """Read owner-specific metadata, never unrelated suggested channel IDs."""
    page = page.replace(r"\u0026", "&").replace(r"\/", "/")
    patterns = (
        r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
        r'<meta[^>]*itemprop=["\']channelId["\'][^>]*content=["\'](UC[A-Za-z0-9_-]{22})',
        r'<meta[^>]*content=["\'](UC[A-Za-z0-9_-]{22})["\'][^>]*itemprop=["\']channelId',
        r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\']https?://(?:www\.)?youtube\.com/channel/(UC[A-Za-z0-9_-]{22})',
    )
    for pattern in patterns:
        match = re.search(pattern, page, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


class ChannelPageMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property") == "og:title":
            self.title = (attrs.get("content") or "").strip()


def lookup_channel_metadata(path: str) -> tuple[str, str]:
    """Use channel-level yt-dlp metadata, then cautiously inspect page metadata."""
    url = "https://www.youtube.com" + path
    try:
        info = ytdlp_runtime.extract_info(url, playlistend=1)
        if isinstance(info, dict):
            for candidate in (info.get("channel_id"), info.get("id")):
                if isinstance(candidate, str) and CHANNEL_ID_RE.fullmatch(candidate):
                    title = next((v.strip() for v in (info.get("channel"), info.get("uploader"), info.get("title"))
                                  if isinstance(v, str) and v.strip()), "")
                    if title:
                        return candidate, title
    except Exception as exc:
        logging.warning("yt-dlp channel lookup failed for %s: %s", path, exc)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=20) as response:
            page = response.read(4_000_000).decode("utf-8", errors="replace")
        channel_id = extract_channel_id(page)
        metadata = ChannelPageMetadata()
        metadata.feed(page)
        if channel_id and metadata.title:
            return channel_id, metadata.title
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logging.warning("YouTube channel HTML lookup failed for %s: %s", path, exc)
    raise ValueError("Could not determine this channel's ID and title. Check the channel address and YouTube access.")


def resolve_channel(value: str) -> tuple[str, str]:
    """Resolve only YouTube channel addresses, never fetch arbitrary user URLs."""
    value = value.strip()
    metadata_title = ""
    if CHANNEL_ID_RE.fullmatch(value):
        channel_id = value
    else:
        if HANDLE_RE.fullmatch(value):
            path = "/" + value
        else:
            raw = value if "://" in value else "https://" + value
            parsed = urlparse(raw)
            if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ("youtube.com", "www.youtube.com", "m.youtube.com") or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise ValueError("Enter a YouTube channel URL, @handle or UC channel ID")
            path = parsed.path.rstrip("/")
            if path.endswith("/videos"):
                path = path[:-7]
        match = re.fullmatch(r"/channel/(UC[A-Za-z0-9_-]{22})", path)
        if match:
            channel_id = match.group(1)
        elif re.fullmatch(r"/@[A-Za-z0-9._-]{3,30}", path):
            channel_id, metadata_title = lookup_channel_metadata(path)
        else:
            raise ValueError("Supported formats: channel URL, @handle, or UC channel ID")
    if metadata_title:
        return channel_id, metadata_title
    try:
        entries, title = fetch_feed_info(channel_id)
    except RuntimeError as exc:
        logging.warning("RSS unavailable during channel lookup for %s: %s", channel_id, exc)
        entries, title = [], ""
    if entries and any(entry.channel_id != channel_id for entry in entries):
        raise ValueError("Channel feed ID does not match the resolved channel")
    if not title or title == "Unknown Channel":
        resolved_id, title = lookup_channel_metadata("/channel/" + channel_id)
        if resolved_id != channel_id:
            raise ValueError("Channel metadata ID does not match the requested channel")
    return channel_id, title


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            channel_title TEXT NOT NULL,
            title TEXT NOT NULL,
            published TEXT NOT NULL,
            url TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            file_path TEXT,
            last_error TEXT,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            initialized_at TEXT NOT NULL
        )
        """
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
        channel_id TEXT PRIMARY KEY, title TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
        added_at TEXT NOT NULL)""")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)")}
    for name, definition in (
        ("download_mode", "TEXT NOT NULL DEFAULT 'latest'"),
        ("mode_since", "TEXT NOT NULL DEFAULT ''"),
        ("needs_scan", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {name} {definition}")
    conn.execute("UPDATE subscriptions SET mode_since=? WHERE mode_since=''", (utc_now(),))
    conn.execute("""CREATE TABLE IF NOT EXISTS channel_videos (
        channel_id TEXT NOT NULL, video_id TEXT NOT NULL, title TEXT NOT NULL,
        duration INTEGER, position INTEGER NOT NULL,
        PRIMARY KEY(channel_id,video_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS download_jobs (
        video_id TEXT PRIMARY KEY, download_root TEXT NOT NULL,
        encoding TEXT NOT NULL, source_path TEXT NOT NULL)""")
    plex.init_db(conn)
    channel_assets.init_db(conn)
    conn.commit()
    return conn


def get_download_root(conn: sqlite3.Connection, default: Path) -> Path:
    row = conn.execute("SELECT value FROM settings WHERE key='download_root'").fetchone()
    return Path(row[0]) if row else default


def save_download_root(conn: sqlite3.Connection, value: str) -> Path:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError("Enter an absolute download directory, such as /downloads")
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Download directory cannot contain control characters")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Use an absolute directory without '..' components")
    try:
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        # Test actual write access as the app user, not just permission bits.
        with tempfile.TemporaryFile(dir=path):
            pass
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Cannot write to download directory: {exc}") from exc
    with conn:
        conn.execute("INSERT INTO settings(key,value) VALUES ('download_root',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(path),))
    return path


def get_skip_shorts(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key='skip_shorts'").fetchone()
    return row is None or row[0] != 'false'


def save_skip_shorts(conn: sqlite3.Connection, enabled: bool) -> None:
    if not isinstance(enabled, bool):
        raise ValueError("Skip Shorts must be true or false")
    with conn:
        conn.execute("INSERT INTO settings(key,value) VALUES ('skip_shorts',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(enabled),))
        if not enabled:
            conn.execute("UPDATE videos SET status='pending',last_error=NULL,updated_at=? "
                         "WHERE status='skipped_short'", (utc_now(),))


def is_short(video_id: str) -> bool:
    """Check YouTube's Shorts route, rather than guessing from duration or title."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", video_id):
        raise RuntimeError("Invalid video ID for Shorts lookup")
    request = urllib.request.Request('https://www.youtube.com/shorts/' + video_id,
                                     headers={'User-Agent': USER_AGENT}, method='HEAD')
    with urllib.request.urlopen(request, timeout=20) as response:
        url = urlparse(response.geturl())
        if response.status == 200 and url.hostname in ('www.youtube.com', 'youtube.com', 'm.youtube.com'):
            if url.path == '/shorts/' + video_id:
                return True
            if url.path == '/watch' and parse_qs(url.query).get('v') == [video_id]:
                return False
    raise RuntimeError("YouTube returned an unrecognized Shorts response; will retry")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_feed(channel_id: str, timeout: int = 30) -> list[VideoEntry]:
    return fetch_feed_info(channel_id, timeout)[0]


def fetch_feed_info(channel_id: str, timeout: int = 30) -> tuple[list[VideoEntry], str]:
    urls = [
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
    ]
    last_error: Exception | None = None

    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = response.read()
            root = ET.fromstring(data)
            if root.tag != f"{{{ATOM_NS}}}feed":
                raise ValueError("YouTube returned a non-Atom response")
            feed_id = (root.findtext(f"{{{YT_NS}}}channelId") or "").strip()
            if not feed_id:
                raise ValueError("YouTube returned a feed without a channel ID")
            if feed_id != channel_id:
                raise ValueError("YouTube returned a different channel feed")
            entries = parse_feed(data)
            if any(entry.channel_id != channel_id for entry in entries):
                raise ValueError("YouTube returned entries for a different channel")
            author = root.find(f"{{{ATOM_NS}}}author")
            title = (author.findtext(f"{{{ATOM_NS}}}name") or "").strip() if author is not None else ""
            if not title:
                title = (root.findtext(f"{{{ATOM_NS}}}title") or "").strip()
            if not title and entries:
                title = entries[0].channel_title
            return entries, title
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ET.ParseError, ValueError) as exc:
            last_error = exc
            logging.warning("Feed request failed for %s via %s: %s", channel_id, url, exc)

    raise RuntimeError(f"Unable to fetch feed for {channel_id}: {last_error}")


def parse_feed(data: bytes) -> list[VideoEntry]:
    root = ET.fromstring(data)
    entries: list[VideoEntry] = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        video_id = (entry.findtext(f"{{{YT_NS}}}videoId") or "").strip()
        channel_id = (entry.findtext(f"{{{YT_NS}}}channelId") or "").strip()
        title = (entry.findtext(f"{{{ATOM_NS}}}title") or "Untitled").strip()
        published = (entry.findtext(f"{{{ATOM_NS}}}published") or "").strip()
        author = entry.find(f"{{{ATOM_NS}}}author")
        channel_title = "Unknown Channel"
        if author is not None:
            channel_title = (author.findtext(f"{{{ATOM_NS}}}name") or channel_title).strip()

        link_url = ""
        for link in entry.findall(f"{{{ATOM_NS}}}link"):
            if link.attrib.get("rel") == "alternate" and link.attrib.get("href"):
                link_url = link.attrib["href"]
                break
        if not link_url and video_id:
            link_url = f"https://www.youtube.com/watch?v={video_id}"

        if video_id and channel_id and published and link_url:
            entries.append(
                VideoEntry(
                    video_id=video_id,
                    channel_id=channel_id,
                    channel_title=channel_title,
                    title=title,
                    published=published,
                    url=link_url,
                )
            )
    return entries


_INVALID_FS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")


def safe_component(value: str, max_len: int = 180) -> str:
    value = _INVALID_FS.sub("_", value)
    value = _WHITESPACE.sub(" ", value).strip().strip(".")
    if not value:
        value = "Unknown"
    return value[:max_len].rstrip(" .")


def set_download_mode(conn: sqlite3.Connection, channel_id: str, mode: str) -> bool:
    if not isinstance(mode, str) or mode not in DOWNLOAD_MODES:
        raise ValueError("Select all, latest and future, or only future videos")
    with conn:
        row = conn.execute("SELECT download_mode FROM subscriptions WHERE channel_id=?",
                           (channel_id,)).fetchone()
        if row is None:
            return False
        if row[0] != mode:
            conn.execute("UPDATE subscriptions SET download_mode=?, mode_since=?, needs_scan=1 WHERE channel_id=?",
                         (mode, utc_now(), channel_id))
            conn.execute("UPDATE videos SET status='ignored_initial', updated_at=? "
                         "WHERE channel_id=? AND status IN ('pending','failed','skipped_short')", (utc_now(), channel_id))
    return True


def subscription_policy(conn: sqlite3.Connection, channel_id: str):
    return conn.execute("SELECT download_mode,mode_since,needs_scan,title FROM subscriptions "
                        "WHERE channel_id=? AND enabled=1", (channel_id,)).fetchone()


def fetch_upload_history(channel_id: str, title: str, limit: int | None = None) -> list[VideoEntry]:
    """Read uploads, optionally limiting discovery to the recent RSS-sized window."""
    playlist_id = "UU" + channel_id[2:]
    options = {}
    if limit is not None:
        options["playlistend"] = limit
    info = ytdlp_runtime.extract_info("https://www.youtube.com/playlist?list=" + playlist_id, **options)
    if not isinstance(info, dict) or info.get("id") != playlist_id or "entries" not in info:
        raise ValueError("Could not read this channel's upload history")
    entries = []
    for item in info["entries"]:
        if STOP:
            raise RuntimeError("Upload history scan interrupted")
        if not isinstance(item, dict):
            raise ValueError("Incomplete upload history; will retry on the next poll")
        if item.get("channel_id") and item["channel_id"] != channel_id:
            raise ValueError("Upload history channel mismatch")
        video_id = item.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("Invalid video ID in upload history")
        # Private/deleted uploads cannot be downloaded. Dates are resolved by
        # yt-dlp during download rather than guessed from relative dates.
        if item.get("availability") in ("private", "needs_auth", "subscriber_only", "premium_only"):
            continue
        if item.get("title") in ("[Private video]", "[Deleted video]"):
            continue
        entries.append(VideoEntry(video_id, channel_id, title, item.get("title") or "Untitled",
                                  "", "https://www.youtube.com/watch?v=" + video_id,
                                  upload_duration(item.get("duration"))))
    return entries


PROFILE_SCAN_LOCK = threading.Lock()


def upload_duration(value) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 10**9:
        return int(value)
    return None


def profile_subscription(conn, channel_id):
    if not isinstance(channel_id, str) or not CHANNEL_ID_RE.fullmatch(channel_id):
        raise ValueError("Invalid channel")
    row = conn.execute("SELECT title,enabled,added_at FROM subscriptions WHERE channel_id=?",
                       (channel_id,)).fetchone()
    if row is None:
        raise ValueError("Channel not found")
    return row


def scan_channel_profile(conn, channel_id):
    subscription = profile_subscription(conn, channel_id)
    if not subscription[1]:
        raise ValueError("Enable this channel before refreshing its videos")
    if not PROFILE_SCAN_LOCK.acquire(blocking=False):
        raise ValueError("A channel video scan is already running. Try again shortly.")
    try:
        entries = fetch_upload_history(channel_id, subscription[0])
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if profile_subscription(conn, channel_id) != subscription:
                raise ValueError("Channel changed during the scan; try again")
            for position, entry in enumerate(entries):
                conn.execute("""INSERT INTO channel_videos VALUES (?,?,?,?,?)
                    ON CONFLICT(channel_id,video_id) DO UPDATE SET
                    title=excluded.title,duration=excluded.duration,position=excluded.position""",
                    (channel_id, entry.video_id, entry.title, entry.duration, position))
    finally:
        PROFILE_SCAN_LOCK.release()


def channel_profile(conn, channel_id, page=0):
    title, enabled, _ = profile_subscription(conn, channel_id)
    if page < 0 or page > 1000000:
        raise ValueError("Invalid page")
    # Include known RSS/history rows even when they are no longer publicly listed.
    query = """WITH catalog AS (
        SELECT c.video_id,c.title,c.duration,c.position FROM channel_videos c WHERE c.channel_id=?
        UNION ALL SELECT v.video_id,v.title,NULL,-1 FROM videos v WHERE v.channel_id=?
        AND NOT EXISTS (SELECT 1 FROM channel_videos c WHERE c.channel_id=v.channel_id AND c.video_id=v.video_id))
        SELECT c.video_id,c.title,c.duration,v.status FROM catalog c
        LEFT JOIN videos v ON v.video_id=c.video_id AND v.channel_id=?
        ORDER BY c.position,c.video_id"""
    rows = conn.execute(query + " LIMIT 100 OFFSET ?", (channel_id, channel_id, channel_id, page * 100)).fetchall()
    total = conn.execute("""SELECT count(*) FROM (
        SELECT video_id FROM channel_videos WHERE channel_id=?
        UNION SELECT video_id FROM videos WHERE channel_id=?)""", (channel_id, channel_id)).fetchone()[0]
    active = {item["video_id"]: item["status"] for item in active_downloads()}
    items = []
    for video_id, video_title, duration, status in rows:
        state = "not downloaded"
        if status == "downloaded":
            state = "downloaded"
        elif status in ("pending", "failed"):
            state = "queued"
        if video_id in active:
            state = active[video_id]
        items.append({"video_id": video_id, "title": video_title, "duration": duration,
                      "status": state, "url": "https://www.youtube.com/watch?v=" + video_id})
    catalog_saved = bool(conn.execute("SELECT 1 FROM channel_videos WHERE channel_id=? LIMIT 1",
                                      (channel_id,)).fetchone())
    return {"title": title, "enabled": bool(enabled), "videos": items, "total": total, "page": page,
            "catalog_saved": catalog_saved}


def queue_profile_videos(conn, channel_id, video_ids):
    if (not isinstance(video_ids, list) or not 1 <= len(video_ids) <= 100
            or any(not isinstance(vid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) for vid in video_ids)):
        raise ValueError("Select between 1 and 100 valid videos")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        title, enabled, _ = profile_subscription(conn, channel_id)
        if not enabled:
            raise ValueError("Enable this channel before queuing videos")
        for video_id in set(video_ids):
            row = conn.execute("SELECT title FROM channel_videos WHERE channel_id=? AND video_id=?",
                               (channel_id, video_id)).fetchone()
            existing = conn.execute("SELECT channel_id,title FROM videos WHERE video_id=?", (video_id,)).fetchone()
            if (existing and existing[0] != channel_id) or (not row and not existing):
                raise ValueError("Video does not belong to this channel's saved catalog")
            entry = VideoEntry(video_id, channel_id, title, row[0] if row else existing[1], "",
                               "https://www.youtube.com/watch?v=" + video_id)
            queue_entry(conn, entry, True)
            conn.execute("UPDATE videos SET status='pending',updated_at=? WHERE video_id=? "
                         "AND status IN ('skipped_short','removed')", (utc_now(), video_id))


def fetch_publication_time(entry: VideoEntry) -> str:
    """Read an exact publish time without extracting media formats or guessing ages."""
    url = "https://www.youtube.com/watch?v=" + entry.video_id
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=20) as response:
        page = response.read(4_000_000).decode("utf-8", errors="replace")
    match = re.search(r"(?:var\s+)?ytInitialPlayerResponse\s*=\s*", page)
    if not match:
        raise ValueError("YouTube did not provide publication metadata")
    data = json.JSONDecoder().raw_decode(page[match.end():])[0]
    details = data.get("videoDetails") or {}
    if details.get("videoId") != entry.video_id or details.get("channelId") != entry.channel_id:
        raise ValueError("Video publication metadata does not match the requested channel/video")
    metadata = (data.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    published = metadata.get("publishDate") or metadata.get("uploadDate")
    if not isinstance(published, str):
        raise ValueError("YouTube did not provide a publication time")
    timestamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("YouTube publication date has no exact time/timezone")
    return timestamp.astimezone(timezone.utc).isoformat()


def fetch_recent_uploads(conn: sqlite3.Connection, channel_id: str, title: str) -> list[VideoEntry]:
    uploads = fetch_upload_history(channel_id, title, limit=15)
    entries = []
    for entry in uploads:
        if STOP:
            raise RuntimeError("Recent upload scan interrupted")
        cached = conn.execute("SELECT published FROM videos WHERE video_id=? AND channel_id=?",
                              (entry.video_id, channel_id)).fetchone()
        try:
            published = cached[0] if cached and cached[0] else fetch_publication_time(entry)
            if datetime.fromisoformat(published.replace("Z", "+00:00")) > datetime.now(timezone.utc):
                continue  # Upcoming releases will be checked again after publication.
            entries.append(replace(entry, published=published))
        except (ValueError, urllib.error.URLError, TimeoutError, OSError) as exc:
            logging.warning("Could not read publication time for %s; will retry: %s", entry.video_id, exc)
    if uploads and not entries:
        raise RuntimeError("No published upload metadata available; will retry on the next poll")
    return entries


def queue_entry(conn: sqlite3.Connection, entry: VideoEntry, selected: bool) -> None:
    """Preserve completed/failed work; allow a broader selection to queue skipped videos."""
    now = utc_now()
    conn.execute("""INSERT INTO videos (
        video_id,channel_id,channel_title,title,published,url,status,first_seen_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET
        published=CASE WHEN videos.published='' THEN excluded.published ELSE videos.published END,
        status=CASE WHEN videos.status='ignored_initial' AND excluded.status='pending'
                    THEN 'pending' ELSE videos.status END""",
        (entry.video_id, entry.channel_id, entry.channel_title, entry.title,
         entry.published, entry.url, "pending" if selected else "ignored_initial", now, now))


def pending_entries(conn: sqlite3.Connection, channel_id: str) -> Iterable[VideoEntry]:
    rows = conn.execute(
        """
        SELECT video_id, channel_id, channel_title, title, published, url
        FROM videos
        WHERE channel_id = ? AND status IN ('pending', 'failed')
        ORDER BY published ASC
        """,
        (channel_id,),
    ).fetchall()
    for row in rows:
        yield VideoEntry(*row)


def download_queue(conn: sqlite3.Connection, include_downloaded: bool = False) -> list[dict]:
    items = active_downloads()
    active_ids = {item["video_id"] for item in items}
    rows = conn.execute("""SELECT v.video_id,v.channel_id,v.channel_title,v.title,s.enabled,v.status
        FROM videos v JOIN subscriptions s ON s.channel_id=v.channel_id
        WHERE v.status IN ('pending','failed')
        ORDER BY s.enabled DESC,s.title COLLATE NOCASE,v.published,v.video_id""").fetchall()
    for video_id, channel_id, channel_title, title, enabled, status in rows:
        if video_id in active_ids:
            continue
        items.append({"video_id": video_id, "channel_id": channel_id, "channel_title": channel_title,
                      "title": title, "status": "queued", "percent": None,
                      "channel_enabled": bool(enabled), "retry": status == "failed"})
    if include_downloaded:
        completed = conn.execute("""SELECT video_id,channel_id,channel_title,title FROM videos
            WHERE status='downloaded' ORDER BY updated_at DESC,video_id""").fetchall()
        for video_id, channel_id, channel_title, title in completed:
            if video_id in active_ids:
                continue
            items.append({"video_id": video_id, "channel_id": channel_id, "channel_title": channel_title,
                          "title": title, "status": "downloaded", "percent": 100.0})
    return items


def remove_download(conn: sqlite3.Connection, video_id: str) -> bool:
    """Delete only the recorded completed file; retain its ID to prevent downloading again."""
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", video_id):
        raise ValueError("Invalid video ID")
    if any(item["video_id"] == video_id for item in active_downloads()):
        raise ValueError("This video is still downloading; try again when it finishes")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT status,file_path FROM videos WHERE video_id=?", (video_id,)).fetchone()
        if row is None:
            conn.rollback()
            return False
        if row[0] != "downloaded":
            raise ValueError("Only completed downloads can be removed")
        if row[1]:
            path = Path(row[1])
            if not path.is_absolute() or ".." in path.parts or path.is_symlink() or path.parent.resolve() != path.parent:
                raise ValueError("Recorded download path is unsafe to remove")
            try:
                path.unlink()  # Missing files are already removed; never delete directories recursively.
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ValueError(f"Could not remove video file: {exc}") from exc
        conn.execute("UPDATE videos SET status='removed',file_path=NULL,updated_at=? WHERE video_id=?",
                     (utc_now(), video_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def stream_download(cmd: list[str]) -> str | None:
    """Consume machine-readable progress as it arrives, retaining bounded diagnostics."""
    output = deque(maxlen=40)
    file_path = None
    with subprocess.Popen(cmd, text=True, encoding="utf-8", errors="replace",
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1) as process:
        for line in process.stdout:
            line = line.strip()
            progress = re.fullmatch(r"pullarr-progress:\s*(\d+(?:\.\d+)?)%", line)
            if progress:
                DOWNLOAD_PROGRESS.update(float(progress.group(1)))
            elif line.startswith("pullarr-file:"):
                try:
                    candidate = json.loads(line.removeprefix("pullarr-file:"))
                    if isinstance(candidate, str):
                        file_path = candidate
                except ValueError:
                    logging.warning("Invalid output path from yt-dlp")
            elif line:
                output.append(line[-4000:])
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, cmd, output="\n".join(output)[-4000:])
    return file_path


def build_output_template(root: Path, entry: VideoEntry) -> str:
    channel_name = safe_component(entry.channel_title)
    channel_dir = channel_name.replace("%", "%%")
    try:
        date = datetime.strptime(entry.release_date, "%Y-%m-%d")
        year = date.strftime("%Y")
        episode = date.strftime("S%YE%m%d")
    except ValueError:
        # Archive listings omit dates; yt-dlp fills these from video metadata.
        year = "%(upload_date>%Y)s"
        episode = "S%(upload_date>%Y)sE%(upload_date>%m%d)s"
    title = safe_component(entry.title, max_len=max(1, 220 - len(channel_name) - 16)).replace("%", "%%")
    filename = f"{channel_dir} - {episode} - {title}.%(ext)s"
    return str(Path(str(root).replace("%", "%%")) / channel_dir / f"Season {year}" / filename)


@dataclass(frozen=True)
class DownloadedVideo:
    entry: VideoEntry
    root: Path
    mode: str
    source: Path


def staged_download(conn: sqlite3.Connection, entry: VideoEntry, root: Path) -> DownloadedVideo | None:
    """Resume a confirmed source, or an older published conversion proven by its ready hard link."""
    row = conn.execute("SELECT download_root,encoding,source_path FROM download_jobs WHERE video_id=?",
                       (entry.video_id,)).fetchone()
    if row:
        saved_root, mode, source = Path(row[0]), row[1], Path(row[2])
        if (mode not in video_encoding.MODES or not saved_root.is_absolute() or not source.is_absolute()
                or not source.resolve().is_relative_to(saved_root.resolve())):
            raise RuntimeError('Saved download job contains an invalid path or encoding')
        if source.is_file() or (mode != 'original' and video_encoding.ready_path(source, mode).is_file()):
            return DownloadedVideo(entry, saved_root, mode, source)
        logging.warning('Saved source missing for %s; downloading again', entry.video_id)
        with conn:
            conn.execute("DELETE FROM download_jobs WHERE video_id=?", (entry.video_id,))
    recovered = []
    for mode in ('x264', 'x265'):
        staging = root / '.pullarr-partial' / mode / entry.video_id
        if not staging.resolve().is_relative_to(root.resolve()):
            raise RuntimeError('Conversion staging directory is outside the download directory')
        for ready in staging.glob('**/*.' + mode + '.ready.mkv'):
            if not ready.resolve().is_relative_to(staging.resolve()):
                continue
            source = ready.with_name(ready.name.removesuffix('.' + mode + '.ready.mkv'))
            destination = root / source.relative_to(staging).with_suffix('.mkv')
            if ready.is_file() and destination.is_file() and os.path.samefile(ready, destination):
                recovered.append(DownloadedVideo(entry, root, mode, source))
    if len(recovered) > 1:
        raise RuntimeError('Multiple published conversions found for this video; check staging files')
    return recovered[0] if recovered else None


def save_download_job(conn: sqlite3.Connection, job: DownloadedVideo) -> None:
    with conn:
        conn.execute("""INSERT INTO download_jobs(video_id,download_root,encoding,source_path)
            VALUES (?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET
            download_root=excluded.download_root,encoding=excluded.encoding,source_path=excluded.source_path""",
            (job.entry.video_id, str(job.root.resolve()), job.mode, str(job.source.absolute())))


def convert_download(job: DownloadedVideo) -> Path:
    source = job.source.resolve()
    staging_root = (job.root / '.pullarr-partial' / job.mode / job.entry.video_id).resolve()
    if not source.is_relative_to(staging_root):
        raise RuntimeError('Conversion source is outside its staging directory')
    destination = job.root / source.relative_to(staging_root).with_suffix('.mkv')
    return video_encoding.convert(source, destination, job.mode, progress=CONVERSION_PROGRESS.update)


def complete_download(conn: sqlite3.Connection, job: DownloadedVideo, file_path: str) -> None:
    if not Path(file_path).is_file():
        raise RuntimeError("Download/conversion did not produce a confirmed output file")
    if not Path(file_path).with_suffix(".jpg").is_file():
        logging.warning("YouTube thumbnail missing for %s; video saved without episode artwork", job.entry.video_id)
    conn.execute("""UPDATE videos SET status='downloaded',attempts=attempts+1,
        file_path=?,last_error=NULL,updated_at=? WHERE video_id=?""",
        (file_path, utc_now(), job.entry.video_id))
    conn.execute("DELETE FROM download_jobs WHERE video_id=?", (job.entry.video_id,))
    try:
        channel_assets.episode_nfo(Path(file_path), job.entry.title, job.entry.video_id)
    except (OSError, ValueError) as exc:
        logging.warning("Episode metadata pending for %s: %s", job.entry.video_id, exc)
    plex.enqueue_scan(conn, file_path, job.root)
    conn.commit()
    if job.mode != 'original':
        video_encoding.cleanup(job.source, job.mode)
    logging.info("Downloaded %s", file_path)
    notify_plex(conn)


def fail_download(conn: sqlite3.Connection, entry: VideoEntry, exc: Exception) -> None:
    output = (exc.stdout or "").strip() if isinstance(exc, subprocess.CalledProcessError) else ""
    error = output[-4000:] if output else str(exc)
    conn.execute("""UPDATE videos SET status='failed',attempts=attempts+1,
        last_error=?,updated_at=? WHERE video_id=? AND status IN ('pending','failed')""",
        (error, utc_now(), entry.video_id))
    conn.commit()
    logging.error("Download failed for %s: %s", entry.video_id, error)


class ConversionPipeline:
    """One encoder and one next download; only the calling thread writes SQLite."""
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pullarr-encode')
        self.future = None
        self.job = None

    def __enter__(self):
        return self

    def submit(self, job: DownloadedVideo) -> bool:
        # The next source is downloaded, but cannot enter the encoder until it is free.
        self.finish()
        if STOP:
            return False  # Leave the staged source queued for restart.
        self.job = job
        CONVERSION_PROGRESS.start(job.entry, 'converting')
        self.future = self.executor.submit(convert_download, job)
        return True

    def finish(self) -> None:
        if self.future is None:
            return
        try:
            file_path = str(self.future.result())
            complete_download(self.conn, self.job, file_path)
        except (subprocess.CalledProcessError, RuntimeError, OSError) as exc:
            fail_download(self.conn, self.job.entry, exc)
        finally:
            self.future = None
            self.job = None
            CONVERSION_PROGRESS.clear()

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.finish()
        finally:
            self.executor.shutdown(wait=True)


def download_video(config: AppConfig, conn: sqlite3.Connection, entry: VideoEntry,
                   pipeline: ConversionPipeline | None = None) -> bool:
    status = conn.execute("SELECT status FROM videos WHERE video_id=?", (entry.video_id,)).fetchone()
    if status and status[0] == 'downloaded':
        return True
    download_root = get_download_root(conn, config.download_root)
    mode = video_encoding.get_mode(conn)
    staging_root = download_root / '.pullarr-partial' / mode / entry.video_id
    media_root = download_root if mode == 'original' else staging_root
    output_template = build_output_template(Path("."), entry)

    cmd = [
        "--no-playlist",
        "--no-overwrites",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "--format",
        video_encoding.SDR_FORMAT if mode == "x264" else config.video_format,
        "--merge-output-format",
        config.merge_output_format,
        "--output",
        output_template,
        "--paths",
        "home:" + str(media_root),
        "--paths",
        "temp:" + str(download_root / ".pullarr-partial"),
        "--print",
        "after_move:pullarr-file:%(filepath)j",
        "--no-simulate",
        "--newline",
        "--no-color",
        "--progress",
        "--progress-template",
        "download:pullarr-progress:%(progress._percent_str)s",
        entry.url,
    ]

    try:
        job = staged_download(conn, entry, download_root)
        if job is None:
            logging.info("Downloading %s - %s", entry.channel_title, entry.title)
            DOWNLOAD_PROGRESS.start(entry)
            if get_skip_shorts(conn) and is_short(entry.video_id):
                with conn:
                    # Serialize with settings saves after the network request.
                    conn.execute("BEGIN IMMEDIATE")
                    if get_skip_shorts(conn):
                        conn.execute("UPDATE videos SET status='skipped_short',last_error=NULL,updated_at=? "
                                     "WHERE video_id=? AND status IN ('pending','failed')", (utc_now(), entry.video_id))
                        logging.info("Skipped Short %s - %s", entry.channel_title, entry.video_id)
                        return False
            download_root.mkdir(parents=True, exist_ok=True)
            with ytdlp_runtime.command() as executable:
                file_path = stream_download(executable + cmd)
            if not file_path or not Path(file_path).is_file():
                raise RuntimeError("yt-dlp did not produce a confirmed output file")
            job = DownloadedVideo(entry, download_root, mode, Path(file_path))
        else:
            logging.info("Resuming saved download %s locally (%s)", entry.video_id, job.mode)
            mode = job.mode
            file_path = str(job.source)
        save_download_job(conn, job)
        if mode != 'original' and pipeline is not None:
            ready = video_encoding.ready_path(job.source, mode).is_file()
            submitted = pipeline.submit(job)
            if submitted and ready:
                pipeline.finish()  # Commit recovered output before another network operation can block.
            return submitted
        if mode != 'original':
            CONVERSION_PROGRESS.start(entry, 'converting')
            DOWNLOAD_PROGRESS.clear()
            file_path = str(convert_download(job))
        complete_download(conn, job, file_path)
        return True
    except (subprocess.CalledProcessError, RuntimeError, OSError) as exc:
        fail_download(conn, entry, exc)
        return False
    finally:
        DOWNLOAD_PROGRESS.clear()
        if pipeline is None:
            CONVERSION_PROGRESS.clear()


def discover_channel(conn: sqlite3.Connection, channel_id: str, policy: tuple) -> None:
    mode, since, needs_scan, title = policy
    if mode == "all" and needs_scan:
        entries = fetch_upload_history(channel_id, title)
    else:
        try:
            entries = fetch_feed(channel_id)
        except RuntimeError:
            logging.warning("RSS unavailable for %s; checking recent uploads with yt-dlp", channel_id)
            entries = fetch_recent_uploads(conn, channel_id, title)
    if any(entry.channel_id != channel_id for entry in entries):
        raise ValueError("Channel discovery ID mismatch")
    cutoff = datetime.fromisoformat(since)
    latest_id = None
    if mode == "latest" and needs_scan and entries:
        latest_id = max(entries, key=lambda entry: datetime.fromisoformat(
            entry.published.replace("Z", "+00:00"))).video_id
    # Lock only while applying results, never during network requests. Discard
    # stale results if the user changed policy, disabled, or removed the channel.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if subscription_policy(conn, channel_id) != policy:
            conn.rollback()
            return
        for entry in entries:
            is_future = bool(entry.published) and datetime.fromisoformat(
                entry.published.replace("Z", "+00:00")) > cutoff
            selected = mode == "all" or is_future or (mode == "latest" and needs_scan and entry.video_id == latest_id)
            queue_entry(conn, entry, selected)
        if entries:
            conn.execute("UPDATE subscriptions SET title=? WHERE channel_id=? AND title=?",
                         (entries[0].channel_title, channel_id, channel_id))
        conn.execute("UPDATE subscriptions SET needs_scan=0 WHERE channel_id=?", (channel_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def process_channel(config: AppConfig, conn: sqlite3.Connection, channel: ChannelConfig,
                    pipeline: ConversionPipeline | None = None) -> None:
    if not channel.enabled:
        return
    policy = subscription_policy(conn, channel.channel_id)
    if policy is None:
        return
    try:
        discover_channel(conn, channel.channel_id, policy)
    except Exception:
        logging.exception("Video discovery failed for %s; queued downloads can still retry", channel.channel_id)
    for entry in pending_entries(conn, channel.channel_id):
        current = subscription_policy(conn, channel.channel_id)
        if STOP or current is None or current[:2] != policy[:2]:
            break
        if pipeline is None:
            download_video(config, conn, entry)
        else:
            download_video(config, conn, entry, pipeline)


def notify_plex(conn: sqlite3.Connection) -> None:
    try:
        plex.process_pending(conn)
    except Exception as exc:
        # Notification failures must not change completed download status.
        logging.error("Plex notification processing failed (%s); pending scans retained", type(exc).__name__)


def cleanup_idle_downloads(config: AppConfig, conn: sqlite3.Connection) -> None:
    """Reclaim only worker staging files after completion, retaining retry inputs."""
    if STOP or active_downloads():
        return
    try:
        with conn:
            # Keep UI queue/settings writes serialized with the empty-queue check.
            conn.execute("BEGIN IMMEDIATE")
            if download_queue(conn):
                return
            partial = get_download_root(conn, config.download_root) / '.pullarr-partial'
            if partial.is_symlink():
                logging.warning("Refusing to clean symlinked staging directory %s", partial)
                return
            if partial.exists():
                shutil.rmtree(partial)
                logging.info("Empty download queue: removed leftover temporary files from %s", partial)
    except (OSError, sqlite3.Error) as exc:
        logging.warning("Could not clean download staging files; will retry next poll: %s", exc)


def run_once(config: AppConfig, conn: sqlite3.Connection) -> None:
    notify_plex(conn)
    channels = [ChannelConfig(channel_id=row[0]) for row in conn.execute(
        "SELECT channel_id FROM subscriptions WHERE enabled = 1 ORDER BY title COLLATE NOCASE")]
    with ConversionPipeline(conn) as pipeline:
        for channel in channels:
            if STOP:
                break
            try:
                row = conn.execute("SELECT title FROM subscriptions WHERE channel_id=? AND enabled=1",
                                   (channel.channel_id,)).fetchone()
                if row is None:
                    continue
                root = get_download_root(conn, config.download_root)
                channel_assets.sync(conn, channel.channel_id, root / safe_component(row[0]), root)
                process_channel(config, conn, channel, pipeline)
            except Exception:
                logging.exception("Channel check failed for %s", channel.channel_id)
    cleanup_idle_downloads(config, conn)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pullarr</title><style>

:root{color-scheme:dark;font:13px/1.5 system-ui,sans-serif;--red:#ec4e20;--orange:#ff9505;--blue:#016fb9;--plum:#64113f;--green:#111d13;--bg:#0c130f;--surface:#111d13;--raised:#17251c;--line:#2b3a30;--text:#ecf0ec;--muted:#a2b0a6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text)}[hidden]{display:none!important}button,input,select{font:inherit}a{color:#71bdf0}button,a,input,select{outline-offset:4px}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--orange)}button{cursor:pointer;border:1px solid transparent;border-radius:6px;background:var(--orange);color:#171208;font-weight:650;padding:7px 12px}button:disabled,input:disabled,select:disabled{opacity:.55;cursor:default}p,small,.sub{color:var(--muted)}p{margin:8px 0 14px}h1,h2,h3{line-height:1.3}h1{font-size:24px;margin:0 0 6px;letter-spacing:-.6px}h2{font-size:15px;margin:0 0 14px}h3{font-size:13px;margin:0}input:not(.switch){flex:1;min-width:0;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:7px 10px}select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 9px;max-width:100%}.sidebar{width:200px;position:fixed;inset:0 auto 0 0;background:var(--green);border-right:1px solid var(--line);display:flex;flex-direction:column;padding:26px 14px;z-index:2}.brand{display:flex;align-items:center;gap:6px;font-size:21px;font-weight:750;padding:0 0 30px;letter-spacing:-.7px}.brand-version{font-size:10px;font-weight:500;letter-spacing:0;color:var(--muted);white-space:nowrap}.brand-mark{display:grid;place-items:center;width:30px;height:30px;border-radius:8px;background:var(--orange);color:var(--green);font-size:19px}.nav-label{font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:var(--muted);padding:0 12px;margin-bottom:8px}.tabs{display:grid;gap:5px}.tabs a{display:flex;align-items:center;gap:12px;text-decoration:none;color:var(--muted);padding:10px 12px;border-radius:6px;font-weight:550}.tabs a:hover{background:var(--raised);color:var(--text)}.tabs a[aria-current="page"]{background:var(--blue);color:white}.nav-count{margin-left:auto;min-width:20px;padding:1px 5px;border-radius:4px;background:var(--raised);color:var(--text);font-size:10px;font-variant-numeric:tabular-nums;text-align:center}.nav-icon{width:18px;text-align:center;font-size:17px}main{margin-left:200px;max-width:1640px;padding:0 32px 32px}.topbar{height:68px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);margin-bottom:30px}.eyebrow{font-size:10px;color:var(--orange);text-transform:uppercase;letter-spacing:1.7px;margin-bottom:9px}.page-intro{margin-bottom:24px}.panel{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:18px;margin-bottom:18px}.add,.controls{display:flex;align-items:center;gap:9px}.add input{width:100%}.switch{width:48px;height:27px;min-width:48px;max-width:48px;flex:0 0 48px;display:inline-block;margin:0;appearance:none;background:#425247;border-radius:30px;cursor:pointer;position:relative;vertical-align:middle}.switch:before{content:"";position:absolute;top:3px;left:3px;width:21px;height:21px;border-radius:50%;background:white;transition:transform .16s}.switch:checked{background:var(--blue)}.switch:checked:before{transform:translateX(21px)}.remove{background:var(--raised);border-color:var(--line);color:var(--text);font-size:11px;padding:5px 9px}.remove:hover{color:#ffad91;border-color:var(--red)}.name{font-weight:600;overflow-wrap:anywhere}.sub{font-size:11px;overflow-wrap:anywhere}.profile-link{background:none;color:var(--text);padding:0;text-align:left;font-size:inherit}.profile-link:hover{color:#71bdf0}.mode-label{font-size:11px;color:var(--muted)}.mode-label select{margin-left:8px}.video-table{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:12px}caption{text-align:left;color:var(--muted);padding-bottom:12px}th{text-transform:uppercase;letter-spacing:.7px;font-size:10px;color:var(--muted);font-weight:550;background:var(--raised)}th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line)}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:#19291f}td{vertical-align:middle}td:first-child{min-width:200px}td .switch{vertical-align:middle}.download-status{min-width:190px;width:230px}.download-progress{position:relative;height:28px}.download-progress progress{display:block;appearance:none;width:100%;height:100%;margin:0;border:0;border-radius:6px;overflow:hidden;background:var(--bg);accent-color:var(--blue)}.download-progress progress::-webkit-progress-bar{background:var(--bg)}.download-progress progress::-webkit-progress-value{background:var(--blue)}.download-progress progress::-moz-progress-bar{background:var(--blue)}.progress-label{position:absolute;inset:0;display:grid;place-items:center;color:#fff;font-size:11px;font-weight:600;white-space:nowrap;pointer-events:none}.download-status .remove{margin-left:8px}.queue-note{margin-top:3px}.video-table input[type=checkbox]{width:16px;height:16px;accent-color:var(--blue)}.history-toggle{display:flex;align-items:center;gap:10px;font-size:12px}.panel-heading{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:12px}.panel-heading h2{margin:0}#msg,#downloads-message,#settings-message,#plex-message,#home-message,#profile-message{color:#ffb45c}p[role=status]:empty{display:none}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:24px}.stat{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:18px}.stat span{color:var(--muted);font-size:11px}.stat strong{display:block;font-size:30px;line-height:1.4;margin:8px 0;color:var(--orange);font-weight:600}.stat:nth-child(2) strong{color:#71bdf0}.stat:nth-child(3){border-top:2px solid var(--plum)}.stat:nth-child(4){border-top:2px solid var(--red)}.stat small{font-size:10px}.empty{padding:28px 12px;text-align:center;color:var(--muted)}#plex-fields{border:0;padding:0;margin:0;min-width:0}.setting-row{display:grid;grid-template-columns:minmax(180px,1fr) minmax(220px,1.3fr);gap:24px;align-items:center;border-bottom:1px solid var(--line);padding:14px 0}.setting-row:first-child{padding-top:0}.setting-row label{font-weight:550}.setting-row p{font-size:11px;margin:4px 0 0;max-width:540px}.setting-row input:not(.switch){width:100%}.setting-row small{display:block;margin-top:5px}.settings-actions{display:flex;gap:8px;align-items:center;padding-top:16px}.settings-note{font-size:11px}.plex-label{display:block}.profile-actions{margin-bottom:12px}#channel-profile .controls{flex-wrap:wrap}#channel-profile td:first-child{min-width:0}#channel-profile td:nth-child(3){min-width:240px}#profile-back{float:right}#profile-page{color:var(--muted);font-size:11px}
@media(min-width:1500px){main{padding-left:44px;padding-right:44px}}@media(max-width:900px){.sidebar{width:164px}.brand{font-size:17px;padding-left:0}main{margin-left:164px;padding:0 18px 24px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.setting-row{grid-template-columns:1fr;gap:10px}.panel-heading{align-items:flex-start;flex-wrap:wrap}}@media(max-width:600px){.sidebar{width:100%;position:sticky;top:0;padding:10px;border-right:0;border-bottom:1px solid var(--line)}.brand,.nav-label{display:none}.tabs{display:flex;justify-content:space-between;gap:3px}.tabs a{padding:8px 4px;font-size:10px;gap:3px}.nav-count{min-width:16px;padding:1px 3px;font-size:9px}.nav-icon{font-size:14px;width:15px}main{margin-left:0;padding:0 12px 20px}.topbar{height:48px;margin-bottom:20px}.add{flex-wrap:wrap}.add input{flex-basis:100%}.panel{padding:13px}.stats{gap:9px}.stat{padding:13px}.stat strong{font-size:25px}.page-intro{margin-bottom:18px}.video-table{margin:0 -3px}}
</style></head><body><aside class="sidebar"><div class="brand"><span class="brand-mark" aria-hidden="true">↧</span>Pullarr<small class="brand-version">v__APP_VERSION__</small></div><div class="nav-label">Workspace</div><nav class="tabs" aria-label="Main navigation">
<a id="home-tab" href="#home" aria-current="page"><span class="nav-icon" aria-hidden="true">⌂</span>Home</a>
<a id="channels-tab" href="#channels"><span class="nav-icon" aria-hidden="true">▤</span>Channels<span id="channels-count" class="nav-count" title="All subscriptions">—</span></a>
<a id="downloads-tab" href="#downloads"><span class="nav-icon" aria-hidden="true">↓</span>Downloads<span id="downloads-count" class="nav-count" title="Queued and active downloads, including paused channels and retries">—</span></a>
<a id="settings-tab" href="#settings"><span class="nav-icon" aria-hidden="true">⚙</span>Settings</a>
</nav></aside><main><header class="topbar"><span>Workspace <span aria-hidden="true"> / </span><strong id="section-title">Home</strong></span></header>
<section id="home-panel" aria-labelledby="home-tab"><div class="page-intro"><div class="eyebrow">Your library, on autopilot</div><h1>Overview</h1><p>Keep an eye on your channels and the next videos headed to your library.</p></div><p id="home-message" role="status"></p><div class="stats"><div class="stat"><span>Subscribed channels</span><strong id="home-channels">—</strong><small>Your saved subscriptions</small></div><div class="stat"><span>Enabled channels</span><strong id="home-enabled">—</strong><small>Monitoring new uploads</small></div><div class="stat"><span>Queued videos</span><strong id="home-queued">—</strong><small>Includes paused channels and retries</small></div><div class="stat"><span>Active jobs</span><strong id="home-active">—</strong><small>Downloading or converting</small></div></div><div class="panel"><div class="panel-heading"><h2>Queue &amp; activity</h2><a href="#downloads">View downloads →</a></div><div class="video-table"><table><thead><tr><th>Video</th><th>Channel</th><th>Status</th></tr></thead><tbody id="home-activity"><tr><td colspan="3" class="empty">Loading overview…</td></tr></tbody></table></div></div></section>
<section id="channels-panel" aria-labelledby="channels-tab" hidden><div class="panel"><h2>Add channel</h2><form id="form" class="add"><input id="url" autocomplete="off" placeholder="YouTube URL, @handle or UC channel ID" required><select id="download-mode" aria-label="Download videos"><option value="all">All</option><option value="latest" selected>Latest and future</option><option value="future">Only future videos</option></select><button id="add">Add</button></form><p id="msg" role="status"></p></div><div class="panel"><h2>Subscriptions</h2><div class="video-table"><table><thead><tr><th>Channel</th><th>Download selection</th><th>Enabled</th><th>Actions</th></tr></thead><tbody id="channels"></tbody></table></div></div><div id="channel-profile" class="panel" hidden><button id="profile-back" class="remove">Close profile</button><h2 id="profile-title"></h2><p>Browse saved uploads and queue selected videos. Shorts filtering still applies. Enable the channel to refresh or queue videos.</p><div class="controls"><button id="profile-scan">Refresh video list</button><button id="profile-queue" disabled>Queue selected</button></div><p id="profile-message" role="status"></p><div class="video-table"><table><thead><tr><th><input id="profile-all" type="checkbox" aria-label="Select all available videos on this page"></th><th>Video ID</th><th>Title</th><th>Length</th><th>Status</th></tr></thead><tbody id="profile-videos"></tbody></table></div><div class="controls"><button id="profile-prev">Previous</button><span id="profile-page"></span><button id="profile-next">Next</button></div></div></section><section id="downloads-panel" class="panel" aria-labelledby="downloads-tab" hidden><div class="panel-heading"><h2>Queue &amp; downloads</h2><label class="history-toggle"><input id="show-downloaded" type="checkbox" class="switch">Show downloaded videos</label></div><p id="downloads-message" role="status"></p><div class="video-table"><table><thead><tr><th>Video</th><th>Channel</th><th>Status</th></tr></thead><tbody id="downloads"></tbody></table></div></section><section id="settings-panel" class="panel" aria-labelledby="settings-tab" hidden><h2>Settings</h2><form id="settings-form"><div class="setting-row"><div><label for="download-root">Download directory</label><p id="directory-hint">Absolute container path, such as /downloads. Use a writable Docker mount to preserve videos.</p></div><input id="download-root" aria-describedby="directory-hint" placeholder="/downloads" maxlength="1024" required disabled></div><div class="setting-row"><div><label for="skip-shorts">Skip Shorts</label><p>Filter YouTube Shorts. Turning this off queues previously skipped Shorts again.</p></div><input id="skip-shorts" type="checkbox" class="switch" checked disabled></div><div class="setting-row"><div><label for="video-encoding">Video encoding</label><p>x264 uses SDR. x265 preserves HDR10/HLG when available. Conversion needs extra disk space and CPU time.</p></div><select id="video-encoding" disabled><option value="original">Original (no conversion)</option><option value="x264">x264 / H.264 (SDR)</option><option value="x265">x265 / HEVC (preserve HDR)</option></select></div><div class="settings-actions"><button id="save-settings" disabled>Save settings</button><span class="sub">Applies to new downloads. Existing files stay in place.</span></div></form><p id="settings-message" role="status"></p><h2>Plex notifications</h2><p>Request a folder scan after each completed download. Save settings before testing.</p><form id="plex-form"><fieldset id="plex-fields" disabled><div class="setting-row"><div><label for="plex-enabled">Notify Plex after downloads</label><p>Request a library scan when a video completes.</p></div><input id="plex-enabled" type="checkbox" class="switch"></div><div class="setting-row"><label for="plex-server">Plex server URL</label><input id="plex-server" type="url" placeholder="http://plex:32400"></div><div class="setting-row"><label for="plex-token">Plex token</label><div><input id="plex-token" type="password" autocomplete="new-password" placeholder="Enter token"><small id="plex-token-help">A saved token is never displayed. Leave blank to keep it.</small></div></div><div class="setting-row"><label for="plex-library">Library ID</label><input id="plex-library" inputmode="numeric" placeholder="1"></div><div class="setting-row"><div><label for="plex-root">Download directory as Plex sees it</label><p>Maps Pullarr /downloads to Plex /media/youtube, for example. Leave blank if paths match.</p></div><input id="plex-root" placeholder="/media/youtube"></div><div class="settings-actions"><button id="plex-save">Save Plex settings</button><button id="plex-test" type="button">Test connection</button></div></fieldset></form><p id="plex-message" role="status"></p><p id="plex-status"></p></section></main><script>
const $=id=>document.getElementById(id);async function request(path,data){const r=await fetch(path,{cache:'no-store',method:data?'POST':'GET',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined});const body=await r.json();if(!r.ok)throw Error(body.error||'Request failed');return body}
async function refresh(){try{const channels=await request('/api/channels');$('channels-count').textContent=channels.length;$('channels').replaceChildren();if(!channels.length){emptyTable('channels',4,'No channels yet. Add your first subscription above.');return}for(const c of channels){const row=document.createElement('tr');const left=document.createElement('td');const name=document.createElement('button');name.className='name';name.textContent=c.title;name.className='name profile-link';name.type='button';name.onclick=()=>openProfile(c);const id=document.createElement('div');id.className='sub';id.textContent=c.channel_id;left.append(name,id);const label=document.createElement('td');const select=document.createElement('select');select.className='download-mode';select.setAttribute('aria-label','Download selection for '+c.title);for(const [value,text] of [['all','All'],['latest','Latest and future'],['future','Only future videos']]){const option=document.createElement('option');option.value=value;option.textContent=text;select.append(option)}select.value=c.download_mode;select.onchange=async()=>{const value=select.value;select.disabled=true;try{await request('/api/download-mode',{channel_id:c.channel_id,download_mode:value});c.download_mode=value;$('msg').textContent='Download selection saved.'}catch(e){select.value=c.download_mode;$('msg').textContent=e.message}finally{select.disabled=false}};label.append(select);const toggle=document.createElement('input');toggle.type='checkbox';toggle.className='switch';toggle.checked=c.enabled;toggle.setAttribute('aria-label','Enable '+c.title);toggle.onchange=async()=>{toggle.disabled=true;try{await request('/api/toggle',{channel_id:c.channel_id,enabled:toggle.checked})}catch(e){toggle.checked=!toggle.checked;$('msg').textContent=e.message}finally{toggle.disabled=false}};const enabled=document.createElement('td');enabled.append(toggle);const controls=document.createElement('td');const remove=document.createElement('button');remove.className='remove';remove.type='button';remove.textContent='Remove';remove.setAttribute('aria-label','Remove '+c.title);remove.onclick=async()=>{if(!confirm('Remove '+c.title+' from Pullarr? Downloaded files will be kept.'))return;remove.disabled=true;try{await request('/api/remove',{channel_id:c.channel_id});await refresh()}catch(e){$('msg').textContent=e.message;remove.disabled=false}};controls.append(remove);row.append(left,label,enabled,controls);$('channels').append(row)}}catch(e){$('msg').textContent=e.message}}
let profileChannel=null,profilePage=0,profileBusy=false,profileRows=[];
const profileSelection=new Set();
function updateProfileSelection(){
    $('profile-queue').disabled=profileBusy||!profileSelection.size;
    const boxes=[...$('profile-videos').querySelectorAll('input:not(:disabled)')];
    $('profile-all').disabled=profileBusy||!boxes.length;
    $('profile-all').checked=boxes.length>0&&boxes.every(box=>box.checked);
    $('profile-all').indeterminate=boxes.some(box=>box.checked)&&!$('profile-all').checked;
}
function profileControls(){
    $('profile-scan').disabled=profileBusy||!profileChannel?.enabled;
    $('profile-prev').disabled=profileBusy||profilePage===0;
    $('profile-next').disabled=profileBusy||!profileHasNext;
    updateProfileSelection();
}
let profileHasNext=false,profileCatalogSaved=false;
function profileLength(duration){
    if(duration===null)return 'Unknown';
    const minutes=String(Math.floor(duration/60)%60).padStart(2,'0');
    const seconds=String(duration%60).padStart(2,'0');
    const hours=duration>=3600?Math.floor(duration/3600)+':':'';
    return hours+minutes+':'+seconds;
}
function profileRow(item,enabled){
    const row=document.createElement('tr'),selection=document.createElement('td'),box=document.createElement('input');
    box.type='checkbox';box.disabled=!enabled||item.status!=='not downloaded';
    box.setAttribute('aria-label','Select '+item.title);
    if(box.disabled)profileSelection.delete(item.video_id);
    box.checked=profileSelection.has(item.video_id);
    box.onchange=()=>{if(box.checked)profileSelection.add(item.video_id);else profileSelection.delete(item.video_id);updateProfileSelection()};
    selection.append(box);row.append(selection);
    const id=document.createElement('td');id.textContent=item.video_id;
    const title=document.createElement('td'),link=document.createElement('a');
    link.textContent=item.title;link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';title.append(link);
    const length=document.createElement('td');length.textContent=profileLength(item.duration);
    const status=document.createElement('td');status.textContent=item.status;
    row.append(id,title,length,status);
    return row;
}
async function loadProfile(){
    if(!profileChannel)return;
    const id=profileChannel.channel_id,page=profilePage;
    const result=await request('/api/channel-videos?channel_id='+encodeURIComponent(id)+'&page='+page);
    if(profileChannel?.channel_id!==id||profilePage!==page)return;
    profileChannel.enabled=result.enabled;profileCatalogSaved=result.catalog_saved;profileRows=result.videos;profileHasNext=(page+1)*100<result.total;
    $('profile-title').textContent=result.title;
    $('profile-page').textContent=result.total?`${page*100+1}–${page*100+result.videos.length} of ${result.total}`:'No videos saved yet.';
    $('profile-videos').replaceChildren(...profileRows.map(item=>profileRow(item,result.enabled)));
    profileControls();
}
async function profileAction(scan){
    if(profileBusy||!profileChannel)return;
    profileBusy=true;profileControls();
    $('profile-message').textContent=scan?'Reading upload history… This can take several minutes for large channels.':'Queuing selected videos…';
    try{
        await request('/api/channel-videos/'+(scan?'scan':'queue'),{channel_id:profileChannel.channel_id,...(scan?{}:{video_ids:[...profileSelection]})});
        profileSelection.clear();await loadProfile();
        $('profile-message').textContent=scan?'Video list updated.':'Selected videos queued.';
    }catch(e){$('profile-message').textContent=e.message}
    finally{profileBusy=false;profileControls()}
}
async function openProfile(channel){
    if(profileBusy)return;
    profileBusy=true;profileChannel=channel;profilePage=0;profileRows=[];profileSelection.clear();profileControls();
    $('channel-profile').hidden=false;$('profile-title').textContent=channel.title;
    $('profile-videos').replaceChildren();$('profile-message').textContent='Loading saved videos…';
    try{await loadProfile();$('profile-message').textContent=''}
    catch(e){$('profile-message').textContent=e.message}
    finally{profileBusy=false;profileControls()}
    if(!profileCatalogSaved&&channel.enabled)await profileAction(true)
    $('channel-profile').scrollIntoView({behavior:'smooth',block:'start'});
}
$('profile-back').onclick=()=>{if(profileBusy)return;profileChannel=null;profileSelection.clear();$('channel-profile').hidden=true};
$('profile-scan').onclick=()=>profileAction(true);
$('profile-queue').onclick=()=>profileAction(false);
$('profile-all').onchange=()=>{const checked=$('profile-all').checked;for(const box of $('profile-videos').querySelectorAll('input:not(:disabled)')){box.checked=checked;box.onchange()}};
async function profileMove(delta){if(profileBusy)return;profileBusy=true;profilePage+=delta;profileSelection.clear();profileControls();try{await loadProfile()}catch(e){$('profile-message').textContent=e.message}finally{profileBusy=false;profileControls()}}
$('profile-prev').onclick=()=>profileMove(-1);$('profile-next').onclick=()=>profileMove(1);
setInterval(()=>{if(currentTab==='channels'&&profileChannel&&!profileBusy&&!document.hidden)loadProfile().catch(e=>{$('profile-message').textContent=e.message})},5000);
const pageVersion='__APP_VERSION__';
const pageUrl=new URL(window.location.href);
if(pageUrl.searchParams.has('v')||pageUrl.searchParams.has('_refresh')){
    pageUrl.searchParams.delete('v');
    pageUrl.searchParams.delete('_refresh');
    window.history.replaceState(window.history.state,'',pageUrl.href);
}
let versionCheckBusy=false;
let versionReloading=false;
async function clearBrowserCaches(){
    if(!('caches' in window))return;
    let timeout;
    try{
        await Promise.race([
            (async()=>{
                const keys=await window.caches.keys();
                await Promise.all(keys.map(async key=>{
                    try{await window.caches.delete(key)}
                    catch(error){console.warn('Could not clear Pullarr browser cache',error)}
                }));
            })(),
            new Promise((resolve,reject)=>{timeout=setTimeout(()=>reject(Error('Browser cache cleanup timed out')),3000)})
        ]);
    }catch(error){console.warn('Could not clear Pullarr browser caches',error)}
    finally{clearTimeout(timeout)}
}
async function checkVersion(){
    if(document.hidden||versionCheckBusy||versionReloading)return;
    versionCheckBusy=true;
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),10000);
    try{
        const response=await fetch('/api/version?current='+encodeURIComponent(pageVersion),{cache:'no-store',signal:controller.signal});
        if(!response.ok)throw Error('Version check failed: '+response.status);
        const data=await response.json();
        if(typeof data.version!=='string'||!/^[0-9]+[.][0-9]+[.][0-9]+$/.test(data.version))throw Error('Invalid server version');
        if(data.version!==pageVersion){
            // The server requests HTTP cache clearing; bound Cache Storage cleanup so refresh always proceeds.
            await clearBrowserCaches();
            window.location.reload();
            versionReloading=true;
            return;
        }
    }catch(error){console.warn('Pullarr version check will retry',error)}
    finally{clearTimeout(timeout);versionCheckBusy=false}
}
setInterval(checkVersion,30000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkVersion()});
window.addEventListener('pageshow',checkVersion);
checkVersion();
const tabs=['home','channels','downloads','settings'];
let currentTab='home';
let settingsLoaded=false;
let settingsBusy=false;
let downloadsBusy=false;
let removingDownload=false;
async function removeDownloaded(item,remove){
    if(removingDownload||!confirm('Delete the downloaded file for “'+item.title+'”? This cannot be undone. Pullarr will remember it so it is not downloaded again.'))return;
    removingDownload=true;remove.disabled=true;
    try{
        await request('/api/downloads/remove',{video_id:item.video_id});
        removingDownload=false;await refreshDownloads();
    }catch(e){$('downloads-message').textContent=e.message}
    finally{removingDownload=false;remove.disabled=false}
}
function activeProgress(item,state){
    const converting=item.status==='converting';
    const percent=Number.isFinite(item.percent)?Math.max(0,Math.min(100,item.percent)):null;
    const wrapper=document.createElement('div');wrapper.className='download-progress';
    const label=document.createElement('span');label.className='progress-label';
    label.textContent=(converting?'Converting':'Downloading')+(percent===null?'…':' '+Number(percent.toFixed(1))+'%');
    label.setAttribute('aria-hidden','true');
    const bar=document.createElement('progress');bar.max=100;
    if(percent!==null)bar.value=percent;
    bar.setAttribute('aria-label',(converting?'Conversion':'Download')+' progress for '+item.title);
    bar.setAttribute('aria-valuetext',label.textContent);
    wrapper.append(bar,label);state.append(wrapper);
}
function downloadState(item){
    const state=document.createElement('td');state.className='download-status';
    if(item.status==='downloading'||item.status==='converting'){
        activeProgress(item,state);return state;
    }
    if(item.status==='downloaded'){
        state.textContent='Downloaded';
        const remove=document.createElement('button');remove.className='remove';remove.type='button';
        remove.textContent='Remove';remove.setAttribute('aria-label','Remove downloaded video '+item.title);
        remove.onclick=()=>removeDownloaded(item,remove);
        state.append(remove);return state;
    }
    state.textContent='Queued';
    if(!item.channel_enabled||item.retry){
        const note=document.createElement('div');note.className='sub queue-note';
        note.textContent=!item.channel_enabled?'Channel disabled':'Waiting to retry';state.append(note);
    }
    return state;
}
async function refreshDownloads(){
    if(downloadsBusy||removingDownload)return;
    downloadsBusy=true;
    const includeDownloaded=$('show-downloaded').checked;
    try{
        const items=await request('/api/downloads'+(includeDownloaded?'?include_downloaded=1':''));
        if(includeDownloaded!==$('show-downloaded').checked)return;
        $('downloads-message').textContent='';
        $('downloads-count').textContent=items.filter(item=>item.status!=='downloaded').length;
        const rows=[];
        for(const item of items){
            const row=document.createElement('tr');
            const details=document.createElement('td');
            details.className='name';details.textContent=item.title;
            const channel=document.createElement('td');channel.textContent=item.channel_title;
            const state=downloadState(item);
            row.append(details,channel,state);rows.push(row);
        }
        $('downloads').replaceChildren(...rows);
        if(!rows.length)emptyTable('downloads',3,includeDownloaded?'No downloads to show.':'No queued downloads.');
    }catch(e){$('downloads-message').textContent='Could not update downloads: '+e.message}
    finally{downloadsBusy=false;if(includeDownloaded!==$('show-downloaded').checked)refreshDownloads()}
}
$('show-downloaded').onchange=()=>refreshDownloads();
function selectTab(name){
    currentTab=name;
    for(const tab of tabs){
        const selected=tab===name;
        if(selected)$(tab+'-tab').setAttribute('aria-current','page');
        else $(tab+'-tab').removeAttribute('aria-current');
        $(tab+'-panel').hidden=!selected;
    }
    $('section-title').textContent=name[0].toUpperCase()+name.slice(1);
    if(name==='home')refreshOverview();
    if(name==='channels')refresh();
    if(name==='downloads')refreshDownloads();
    if(name==='settings'){if(!settingsLoaded)loadSettings();if(!plexLoaded)loadPlex();else refreshPlexStatus()}
}
function navigate(){const name=window.location.hash.slice(1);selectTab(tabs.includes(name)?name:'home')}
window.addEventListener('hashchange',navigate);
function emptyTable(id,columns,message){
    const row=document.createElement('tr'),cell=document.createElement('td');
    cell.colSpan=columns;cell.className='empty';cell.textContent=message;row.append(cell);$(id).replaceChildren(row);
}
function overviewRow(item){
    const row=document.createElement('tr');
    let status=item.status;
    if(Number.isFinite(item.percent))status+=' · '+item.percent.toFixed(1)+'%';
    if(!item.channel_enabled&&item.status==='queued')status+=' · channel disabled';
    else if(item.retry)status+=' · waiting to retry';
    for(const text of [item.title,item.channel_title,status]){
        const cell=document.createElement('td');cell.textContent=text;row.append(cell);
    }
    return row;
}
let overviewBusy=false;
async function refreshOverview(){
    if(overviewBusy)return;overviewBusy=true;
    try{
        const [channels,items]=await Promise.all([request('/api/channels'),request('/api/downloads')]);
        $('channels-count').textContent=channels.length;
        $('downloads-count').textContent=items.length;
        $('home-channels').textContent=channels.length;
        $('home-enabled').textContent=channels.filter(c=>c.enabled).length;
        $('home-queued').textContent=items.filter(item=>item.status==='queued').length;
        $('home-active').textContent=items.filter(item=>['downloading','converting'].includes(item.status)).length;
        const rows=items.slice(0,8).map(overviewRow);
        $('home-activity').replaceChildren(...rows);
        if(!rows.length)emptyTable('home-activity',3,'Your queue is clear. New uploads will appear here.');
        $('home-message').textContent='';
    }catch(e){$('home-message').textContent='Could not update overview: '+e.message}
    finally{overviewBusy=false}
}
setInterval(()=>{if(!document.hidden)refreshOverview()},5000);
async function loadSettings(){
    if(settingsBusy)return;
    settingsBusy=true;$('settings-message').textContent='Loading settings…';
    try{
        const settings=await request('/api/settings');
        $('download-root').value=settings.download_root;$('video-encoding').value=settings.video_encoding;$('skip-shorts').checked=settings.skip_shorts;
        settingsLoaded=true;$('video-encoding').disabled=false;$('skip-shorts').disabled=false;$('download-root').disabled=false;$('save-settings').disabled=false;
        $('settings-message').textContent='';
    }catch(e){$('settings-message').textContent='Could not load settings: '+e.message}
    finally{settingsBusy=false}
}
$('settings-form').onsubmit=async e=>{
    e.preventDefault();if(settingsBusy||!settingsLoaded)return;
    settingsBusy=true;$('video-encoding').disabled=true;$('skip-shorts').disabled=true;$('save-settings').disabled=true;$('download-root').disabled=true;
    $('settings-message').textContent='Saving…';
    try{
        const settings=await request('/api/settings',{download_root:$('download-root').value,video_encoding:$('video-encoding').value,skip_shorts:$('skip-shorts').checked});
        $('download-root').value=settings.download_root;$('video-encoding').value=settings.video_encoding;$('skip-shorts').checked=settings.skip_shorts;
        $('settings-message').textContent='Settings saved.';
    }catch(e){$('settings-message').textContent=e.message}
    finally{settingsBusy=false;$('video-encoding').disabled=false;$('skip-shorts').disabled=false;$('save-settings').disabled=false;$('download-root').disabled=false}
};
let plexLoaded=false;
let plexBusy=false;
let plexStatusBusy=false;
function showPlexStatus(settings){
    $('plex-token').placeholder=settings.has_token?'Token saved — leave blank to keep':'Enter token';
    $('plex-status').textContent='Pending scan requests: '+settings.pending_scans+(settings.last_result?' · '+settings.last_result.message:'');
}
async function refreshPlexStatus(){
    if(plexBusy||plexStatusBusy||!plexLoaded)return;plexStatusBusy=true;
    try{showPlexStatus(await request('/api/plex'))}
    catch(e){$('plex-status').textContent='Could not update Plex status: '+e.message}
    finally{plexStatusBusy=false}
}
async function loadPlex(){
    if(plexBusy)return;plexBusy=true;
    try{
        const settings=await request('/api/plex');
        $('plex-enabled').checked=settings.enabled;$('plex-server').value=settings.server_url;
        $('plex-library').value=settings.library_id;$('plex-root').value=settings.plex_root;
        showPlexStatus(settings);plexLoaded=true;$('plex-fields').disabled=false;
    }catch(e){$('plex-message').textContent='Could not load Plex settings: '+e.message}
    finally{plexBusy=false}
}
$('plex-form').onsubmit=async e=>{
    e.preventDefault();if(plexBusy||!plexLoaded)return;
    plexBusy=true;$('plex-fields').disabled=true;$('plex-message').textContent='Saving…';
    try{
        const settings=await request('/api/plex',{enabled:$('plex-enabled').checked,server_url:$('plex-server').value,
            token:$('plex-token').value,library_id:$('plex-library').value,plex_root:$('plex-root').value});
        $('plex-token').value='';showPlexStatus(settings);$('plex-message').textContent='Plex settings saved.';
    }catch(e){$('plex-message').textContent=e.message}
    finally{plexBusy=false;$('plex-fields').disabled=false}
};
$('plex-test').onclick=async()=>{
    if(plexBusy||!plexLoaded)return;
    plexBusy=true;$('plex-fields').disabled=true;$('plex-message').textContent='Testing saved settings…';
    try{const result=await request('/api/plex/test',{});$('plex-message').textContent=result.message}
    catch(e){$('plex-message').textContent=e.message}
    finally{plexBusy=false;$('plex-fields').disabled=false}
};
setInterval(()=>{if(document.hidden)return;if(currentTab==='downloads')refreshDownloads();if(currentTab==='settings')refreshPlexStatus()},2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&currentTab==='downloads')refreshDownloads()});
$('form').onsubmit=async e=>{e.preventDefault();$('add').disabled=true;$('msg').textContent='Resolving channel…';try{await request('/api/channels',{value:$('url').value,download_mode:$('download-mode').value});$('url').value='';$('msg').textContent='Channel added.';await refresh()}catch(e){$('msg').textContent=e.message}finally{$('add').disabled=false}};navigate();refreshOverview();
</script></body></html>""".replace("__APP_VERSION__", VERSION)


class Handler(BaseHTTPRequestHandler):
    default_download_root: Path = Path("/downloads")
    db_path: Path = Path("/config/state.db")

    def send(self, status, content, content_type="application/json", clear_cache=False):
        payload = content if isinstance(content, bytes) else json.dumps(content).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        if clear_cache:
            self.send_header("Clear-Site-Data", '"cache"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline' 'self'; style-src 'unsafe-inline' 'self'")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/version":
            current = parse_qs(parsed.query).get("current", [VERSION])[0]
            return self.send(200, {"version": VERSION}, clear_cache=current != VERSION)
        if parsed.path == "/":
            return self.send(200, PAGE.encode(), "text/html")
        if self.path == "/api/plex":
            with closing(sqlite3.connect(self.db_path)) as conn:
                settings = plex.public_settings(conn)
            return self.send(200, settings)
        if self.path == "/api/settings":
            with closing(sqlite3.connect(self.db_path)) as conn:
                root = get_download_root(conn, self.default_download_root)
                mode = video_encoding.get_mode(conn)
                skip_shorts = get_skip_shorts(conn)
            return self.send(200, {"download_root": str(root), "video_encoding": mode, "skip_shorts": skip_shorts})
        if parsed.path == "/api/downloads":
            include_downloaded = parse_qs(parsed.query).get("include_downloaded") == ["1"]
            with closing(sqlite3.connect(self.db_path)) as conn:
                items = download_queue(conn, include_downloaded)
            return self.send(200, items)
        if parsed.path == "/api/channel-videos":
            try:
                params = parse_qs(parsed.query)
                with closing(sqlite3.connect(self.db_path)) as conn:
                    result = channel_profile(conn, params.get("channel_id", [None])[0],
                                             int(params.get("page", ["0"])[0]))
                return self.send(200, result)
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
        if self.path == "/api/channels":
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                rows = conn.execute("SELECT channel_id,title,enabled,download_mode FROM subscriptions ORDER BY title COLLATE NOCASE").fetchall()
            return self.send(200, [{"channel_id": a, "title": b, "enabled": bool(c), "download_mode": d} for a,b,c,d in rows])
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        if self.path not in ("/api/channel-videos/scan", "/api/channel-videos/queue", "/api/channels", "/api/toggle", "/api/remove", "/api/download-mode", "/api/settings", "/api/downloads/remove", "/api/plex", "/api/plex/test"):
            return self.send(404, {"error": "Not found"})
        # Prevent cross-origin browser forms from modifying settings.
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", "") and origin != "https://" + self.headers.get("Host", ""):
            return self.send(403, {"error": "Origin not allowed"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(415, {"error": "JSON required"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 4096:
                return self.send(400, {"error": "Invalid request size"})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Invalid request")
            if self.path == "/api/plex":
                with closing(sqlite3.connect(self.db_path, timeout=30)) as conn:
                    settings = plex.save_settings(conn, data)
                WAKE.set()
                return self.send(200, settings)
            if self.path == "/api/plex/test":
                with closing(sqlite3.connect(self.db_path)) as conn:
                    message = plex.test_connection(conn)
                return self.send(200, {"message": message})
            if self.path == "/api/downloads/remove":
                with closing(sqlite3.connect(self.db_path, timeout=30)) as conn:
                    removed = remove_download(conn, data.get("video_id"))
                if not removed:
                    return self.send(404, {"error": "Video not found"})
                return self.send(200, {"ok": True})
            if self.path == "/api/settings":
                with closing(sqlite3.connect(self.db_path, timeout=30)) as conn:
                    mode = video_encoding.validate_mode(data.get('video_encoding', video_encoding.get_mode(conn)))
                    skip_shorts = data.get('skip_shorts', get_skip_shorts(conn))
                    if not isinstance(skip_shorts, bool):
                        raise ValueError("Skip Shorts must be true or false")
                    root = save_download_root(conn, data.get("download_root"))
                    with conn:
                        conn.execute("INSERT INTO settings(key,value) VALUES ('video_encoding',?) "
                                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (mode,))
                    save_skip_shorts(conn, skip_shorts)
                WAKE.set()
                return self.send(200, {"download_root": str(root), "video_encoding": mode, "skip_shorts": skip_shorts})
            if self.path == "/api/channels":
                value = data.get("value")
                if not isinstance(value, str) or len(value) > 300:
                    raise ValueError("Invalid channel input")
                mode = data.get("download_mode", "latest")
                if not isinstance(mode, str) or mode not in DOWNLOAD_MODES:
                    raise ValueError("Invalid download selection")
                channel_id, title = resolve_channel(value)
                with closing(sqlite3.connect(self.db_path, timeout=30)) as conn, conn:
                    conn.execute("INSERT OR IGNORE INTO subscriptions(channel_id,title,enabled,added_at,mode_since,download_mode) VALUES (?,?,1,?,?,?)", (channel_id,title,utc_now(),utc_now(),mode))
                WAKE.set()
                return self.send(200, {"channel_id": channel_id, "title": title})
            channel_id = data.get("channel_id")
            enabled = data.get("enabled")
            if not isinstance(channel_id, str) or not CHANNEL_ID_RE.fullmatch(channel_id):
                raise ValueError("Invalid channel")
            if self.path in ("/api/channel-videos/scan", "/api/channel-videos/queue"):
                with closing(sqlite3.connect(self.db_path, timeout=30)) as conn:
                    if self.path.endswith("/scan"):
                        scan_channel_profile(conn, channel_id)
                    else:
                        queue_profile_videos(conn, channel_id, data.get("video_ids"))
                        WAKE.set()
                return self.send(200, {"ok": True})
            if self.path == "/api/download-mode":
                with closing(sqlite3.connect(self.db_path, timeout=30)) as conn, conn:
                    changed = set_download_mode(conn, channel_id, data.get("download_mode"))
                if not changed:
                    return self.send(404, {"error": "Channel not found"})
                WAKE.set()
                return self.send(200, {"ok": True})
            if self.path == "/api/remove":
                with closing(sqlite3.connect(self.db_path, timeout=30)) as conn, conn:
                    changed = conn.execute("DELETE FROM subscriptions WHERE channel_id=?", (channel_id,)).rowcount
                if not changed:
                    return self.send(404, {"error": "Channel not found"})
                return self.send(200, {"ok": True})
            if not isinstance(enabled, bool):
                raise ValueError("Invalid channel or toggle")
            with closing(sqlite3.connect(self.db_path, timeout=30)) as conn, conn:
                changed = conn.execute("UPDATE subscriptions SET enabled=? WHERE channel_id=?", (int(enabled),channel_id)).rowcount
            if not changed:
                return self.send(404, {"error": "Channel not found"})
            WAKE.set()
            return self.send(200, {"ok": True})
        except (ValueError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            self.send(400, {"error": str(exc)})
        except Exception:
            logging.exception("Web API error")
            self.send(500, {"error": "Internal server error"})


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    config = AppConfig(
        poll_interval_seconds=max(60, int(os.environ.get("POLL_INTERVAL_SECONDS", "900"))),
        download_root=Path(os.environ.get("DOWNLOAD_ROOT", "/downloads")),
        database_path=Path(os.environ.get("DATABASE_PATH", "/config/state.db")),
    )
    updater_stop = threading.Event()
    threading.Thread(target=ytdlp_runtime.update_loop, args=(updater_stop,), daemon=True).start()
    conn = init_db(config.database_path)
    Handler.db_path = config.database_path
    Handler.default_download_root = config.download_root
    server = ThreadingHTTPServer((os.environ.get("WEB_HOST", "0.0.0.0"), int(os.environ.get("WEB_PORT", "8080"))), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Pullarr v%s started; web UI on port %s", VERSION, server.server_port)
    try:
        while not STOP:
            WAKE.clear()
            run_once(config, conn)
            WAKE.wait(config.poll_interval_seconds)
    finally:
        updater_stop.set()
        server.shutdown()
        server.server_close()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

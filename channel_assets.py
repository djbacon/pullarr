"""Local channel artwork and Plex NFO metadata; never overwrite user sidecars."""
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import plex
import ytdlp_runtime


MAX_IMAGE_BYTES = 10 * 1024 * 1024
_XML_INVALID = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]')


def init_db(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS channel_assets (
        channel_id TEXT NOT NULL, download_root TEXT NOT NULL, next_check TEXT NOT NULL,
        PRIMARY KEY(channel_id, download_root))''')


def xml_text(parent, tag, value, **attributes):
    ET.SubElement(parent, tag, attributes).text = _XML_INVALID.sub('', str(value))


def publish(path, data):
    """Link a fully written file into place, preserving any existing destination."""
    if path.exists() or path.is_symlink():
        return False
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.pullarr-metadata-') as staged:
        staged.write(data)
        staged.flush()
        os.fchmod(staged.fileno(), 0o644)  # Plex may run as a different container UID.
        try:
            os.link(staged.name, path)
        except FileExistsError:
            return False
    return True


def safe_path(path):
    if not path.is_absolute() or '..' in path.parts or path.resolve() != path or path.is_symlink():
        raise ValueError('Metadata destination contains an unsafe path or symlink')


def episode_nfo(video, title, video_id):
    safe_path(video)
    if not video.is_file() or video.with_suffix('.nfo').exists():
        return False
    match = re.search(r' - S(\d{4})E(\d{4}) - ', video.stem)
    if not match:
        raise ValueError('Episode filename does not contain a supported year/month/day number')
    year, month_day = match.groups()
    aired = datetime.strptime(year + month_day, '%Y%m%d').date().isoformat()
    node = ET.Element('episodedetails')
    for tag, value in (('title', title), ('season', int(year)), ('episode', int(month_day)),
                       ('aired', aired), ('plot', 'https://www.youtube.com/watch?v=' + video_id)):
        xml_text(node, tag, value)
    return publish(video.with_suffix('.nfo'), ET.tostring(node, encoding='utf-8', xml_declaration=True))


def channel_info(channel_id):
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id):
        raise ValueError('Invalid canonical channel ID')
    info = ytdlp_runtime.extract_info('https://www.youtube.com/channel/' + channel_id + '/videos',
                                      playlist_items='0')
    if not isinstance(info, dict) or info.get('channel_id') != channel_id or info.get('id') != channel_id:
        raise ValueError('Channel metadata identity mismatch')
    if not isinstance(info.get('channel'), str) or not info['channel'].strip():
        raise ValueError('Channel metadata has no title')
    return info


def artwork_url(info, kind):
    thumbnails = info.get('thumbnails') or []
    if not isinstance(thumbnails, list):
        return None
    # Use owner-specific images, never an arbitrary playlist/video thumbnail.
    for thumbnail in reversed(thumbnails):
        if isinstance(thumbnail, dict) and thumbnail.get('id') == kind + '_uncropped':
            url = thumbnail.get('url')
            return url if isinstance(url, str) else None
    return None


def download_artwork(url, destination):
    parsed = urlparse(url)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not any(host == domain or host.endswith('.' + domain)
                       for domain in ('ytimg.com', 'ggpht.com', 'googleusercontent.com'))):
        raise ValueError('Channel artwork URL is not a trusted YouTube image host')
    opener = urllib.request.build_opener(plex.NoRedirect())
    with opener.open(urllib.request.Request(url, headers={'User-Agent': 'Pullarr'}), timeout=20) as response:
        data = response.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Channel artwork is empty or too large')
    with tempfile.TemporaryDirectory(prefix='pullarr-channel-art-') as temp:
        source = Path(temp) / 'source'
        result = Path(temp) / 'art.jpg'
        source.write_bytes(data)
        subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-protocol_whitelist', 'file,pipe',
                        '-f', 'image2', '-pattern_type', 'none', '-i', str(source), '-frames:v', '1',
                        '-vf', r'scale=min(1920\,iw):min(1920\,ih):force_original_aspect_ratio=decrease',
                        '-update', '1', str(result)], check=True, capture_output=True, timeout=60)
        return publish(destination, result.read_bytes())


def write_channel(folder, info):
    safe_path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    nfo = folder / 'tvshow.nfo'
    if nfo.exists():
        safe_path(nfo)
        existing = ET.parse(nfo).find("uniqueid[@type='youtube']")
        if existing is not None and existing.text != info['channel_id']:
            raise ValueError('Channel folder belongs to a different YouTube channel')
    node = ET.Element('tvshow')
    xml_text(node, 'title', info['channel'].strip())
    description = info.get('description') if isinstance(info.get('description'), str) else ''
    xml_text(node, 'plot', description + '\n\nhttps://www.youtube.com/channel/' + info['channel_id'])
    xml_text(node, 'studio', 'YouTube')
    xml_text(node, 'uniqueid', info['channel_id'], type='youtube', default='true')
    changed = publish(nfo, ET.tostring(node, encoding='utf-8', xml_declaration=True))
    complete = True
    for kind, filename in (('avatar', 'poster.jpg'), ('banner', 'background.jpg')):
        destination = folder / filename
        if destination.exists() or destination.is_symlink():
            continue
        url = artwork_url(info, kind)
        if url:
            try:
                changed = download_artwork(url, destination) or changed
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                complete = False
                logging.warning('Channel %s pending for %s (%s); retry in one hour',
                                kind, info['channel_id'], type(exc).__name__)
        else:
            logging.info('No channel %s available for %s', kind, info['channel_id'])
    return changed, complete


def sync(conn, channel_id, folder, download_root):
    """Backfill enabled channels once daily; failures retry in one hour across restarts."""
    if not conn.execute('SELECT 1 FROM subscriptions WHERE channel_id=? AND enabled=1', (channel_id,)).fetchone():
        return
    now = datetime.now(timezone.utc)
    root = download_root.absolute()
    row = conn.execute('SELECT next_check FROM channel_assets WHERE channel_id=? AND download_root=?',
                       (channel_id, str(root))).fetchone()
    if row and row[0] > now.isoformat():
        return
    delay = timedelta(days=1)
    try:
        safe_path(root)
        folders = {folder.absolute()}
        for video_id, title, file_path in conn.execute(
                "SELECT video_id,title,file_path FROM videos WHERE channel_id=? AND status='downloaded' AND file_path IS NOT NULL",
                (channel_id,)):
            video = Path(file_path)
            if not video.is_relative_to(root) or len(video.relative_to(root).parts) != 3:
                continue  # Never follow old/custom paths outside the selected download root.
            safe_path(video)
            if not video.is_file():
                continue
            folders.add(video.parent.parent)
            try:
                if episode_nfo(video, title, video_id):
                    plex.enqueue_scan(conn, str(video), root)
            except (OSError, ValueError) as exc:
                if isinstance(exc, OSError):
                    delay = timedelta(hours=1)
                logging.warning('Episode metadata pending for %s: %s', video_id, exc)
        conn.commit()  # Do not hold SQLite's write lock during YouTube requests.
        info = channel_info(channel_id)
        # The UI may disable or remove this subscription during the network call.
        if not conn.execute('SELECT 1 FROM subscriptions WHERE channel_id=? AND enabled=1', (channel_id,)).fetchone():
            conn.commit()
            return
        for target in sorted(folders):
            changed, complete = write_channel(target, info)
            if changed:
                plex.enqueue_scan(conn, str(target / 'tvshow.nfo'), root)
                conn.commit()  # Release the write lock before another folder's artwork requests.
            if not complete:
                delay = timedelta(hours=1)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ET.ParseError) as exc:
        delay = timedelta(hours=1)
        logging.warning('Channel metadata pending for %s (%s): %s', channel_id, type(exc).__name__,
                        str(exc) if isinstance(exc, ValueError) else 'will retry in one hour')
    conn.execute('''INSERT INTO channel_assets(channel_id,download_root,next_check) VALUES (?,?,?)
        ON CONFLICT(channel_id,download_root) DO UPDATE SET next_check=excluded.next_check''',
                 (channel_id, str(root), (now + delay).isoformat()))
    conn.commit()

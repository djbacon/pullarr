"""Plex folder scan notifications using only the standard library."""
import json
import logging
import sqlite3
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, urlparse


class PlexError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the authentication header to a redirected destination.
        return None


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS plex_scans (
        directory TEXT PRIMARY KEY, download_root TEXT NOT NULL,
        retry_after TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '')""")


def config(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key='plex'").fetchone()
    return json.loads(row[0]) if row else {
        'enabled': False, 'server_url': '', 'token': '', 'library_id': '', 'plex_root': ''}


def public_settings(conn: sqlite3.Connection) -> dict:
    result = config(conn)
    result['has_token'] = bool(result.pop('token'))
    result['pending_scans'] = conn.execute('SELECT count(*) FROM plex_scans').fetchone()[0]
    row = conn.execute("SELECT value FROM settings WHERE key='plex_result'").fetchone()
    result['last_result'] = json.loads(row[0]) if row else None
    return result


def require_ready(settings: dict) -> None:
    if not all(settings[key] for key in ('server_url', 'token', 'library_id')):
        raise PlexError('Set the Plex server URL, token, and library ID first')


def save_settings(conn: sqlite3.Connection, data: dict) -> dict:
    previous = config(conn)
    enabled = data.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError('Plex enabled must be true or false')
    values = {}
    for key in ('server_url', 'token', 'library_id', 'plex_root'):
        value = data.get(key, '')
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Invalid Plex ' + key.replace('_', ' '))
        values[key] = value.strip()
    values['server_url'] = values['server_url'].rstrip('/')
    if values['server_url']:
        parsed = urlparse(values['server_url'])
        try:
            valid_port = parsed.port is None or 1 <= parsed.port <= 65535
        except ValueError:
            valid_port = False
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not valid_port or any(c.isspace() for c in values['server_url'])):
            raise ValueError('Use a Plex HTTP(S) server URL without credentials, query, or fragment')
    if values['library_id'] and (not values['library_id'].isascii() or not values['library_id'].isdigit()):
        raise ValueError('Plex library ID must be a number')
    if values['plex_root']:
        path = PurePosixPath(values['plex_root'])
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('Plex folder must be an absolute container path without .. components')
        values['plex_root'] = str(path)
    if not values['token']:
        if values['server_url'] != previous['server_url'] and previous['token']:
            raise ValueError('Enter a token when changing the Plex server URL')
        values['token'] = previous['token']
    if any(not 33 <= ord(c) <= 126 for c in values['token']):
        raise ValueError('Invalid Plex token')
    values['enabled'] = enabled
    if enabled:
        require_ready(values)
    with conn:
        conn.execute("INSERT INTO settings(key,value) VALUES ('plex',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(values),))
        conn.execute("DELETE FROM settings WHERE key='plex_result'")
        conn.execute("UPDATE plex_scans SET retry_after='',last_error=''")
    return public_settings(conn)


def request(settings: dict, endpoint: str, read: bool = False) -> bytes:
    require_ready(settings)
    req = urllib.request.Request(settings['server_url'] + endpoint, headers={
        'X-Plex-Token': settings['token'], 'Accept': 'application/xml',
        'X-Plex-Product': 'Pullarr', 'X-Plex-Client-Identifier': 'pullarr',
    })
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(req, timeout=10) as response:
            return response.read(1_000_000) if read else b''
    except urllib.error.HTTPError as exc:
        hints = {401: 'check the token', 403: 'check token access', 404: 'check server URL and library ID'}
        hint = hints.get(exc.code, 'check the Plex server')
        exc.close()
        raise PlexError(f'Plex returned HTTP {exc.code}; {hint}') from None
    except (OSError, ValueError, urllib.error.URLError):
        # Do not expose URLs, request objects, tokens, or untrusted response bodies.
        raise PlexError('Could not reach Plex; check the server URL, network, and TLS certificate') from None


def test_connection(conn: sqlite3.Connection) -> str:
    settings = config(conn)
    payload = request(settings, '/library/sections', read=True)
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        raise PlexError('Plex returned an invalid library list') from None
    if root.tag != 'MediaContainer' or not any(
            node.get('key') == settings['library_id'] for node in root.findall('Directory')):
        raise PlexError('The selected library ID was not found on this Plex server')
    return 'Connected to Plex; selected library is accessible.'


def enqueue_scan(conn: sqlite3.Connection, file_path: str, download_root: Path) -> None:
    if config(conn)['enabled']:
        conn.execute("""INSERT INTO plex_scans(directory,download_root) VALUES (?,?)
            ON CONFLICT(directory) DO UPDATE SET download_root=excluded.download_root""",
            (str(Path(file_path).resolve().parent), str(download_root.resolve())))


def scan_path(settings: dict, directory: str, download_root: str) -> str:
    try:
        relative = Path(directory).relative_to(Path(download_root))
    except ValueError:
        raise PlexError('Completed file is outside its download directory; check the path mapping') from None
    if settings['plex_root']:
        return str(PurePosixPath(settings['plex_root']).joinpath(*relative.parts))
    return directory


def process_pending(conn: sqlite3.Connection) -> None:
    settings = config(conn)
    if not settings['enabled']:
        return
    now = datetime.now(timezone.utc)
    rows = conn.execute('SELECT directory,download_root FROM plex_scans WHERE retry_after<=? ORDER BY directory LIMIT 10',
                        (now.isoformat(),)).fetchall()
    for directory, download_root in rows:
        try:
            path = scan_path(settings, directory, download_root)
            request(settings, '/library/sections/' + settings['library_id'] + '/refresh?' + urlencode({'path': path}))
        except PlexError as exc:
            message = str(exc)
            with conn:
                conn.execute('UPDATE plex_scans SET last_error=?,retry_after=? WHERE directory=?',
                             (message, (now + timedelta(seconds=60)).isoformat(), directory))
                record_result(conn, message)
            logging.warning('Plex scan notification pending: %s', message)
        else:
            with conn:
                conn.execute('DELETE FROM plex_scans WHERE directory=?', (directory,))
                record_result(conn, 'Plex accepted the folder scan request.')
            logging.info('Plex accepted folder scan for %s', path)


def record_result(conn: sqlite3.Connection, message: str) -> None:
    value = json.dumps({'message': message, 'at': datetime.now(timezone.utc).isoformat()})
    conn.execute("INSERT INTO settings(key,value) VALUES ('plex_result',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))

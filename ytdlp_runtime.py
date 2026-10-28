"""Stage daily yt-dlp updates without modifying packages used by active jobs."""
from contextlib import contextmanager
import json
import logging
import subprocess
import sys
import tempfile
import threading
import time

UPDATE_INTERVAL = 24 * 60 * 60
RETRY_INTERVAL = 60 * 60
_CURRENT = None
_LOCK = threading.Lock()
_BOOTSTRAP = "import sys; target = sys.argv.pop(1); sys.path.insert(0, target) if target else None; "
_METADATA = """
import json
from yt_dlp import YoutubeDL
request = json.load(sys.stdin)
with YoutubeDL(request['options']) as ydl:
    info = ydl.extract_info(request['url'], download=False)
    print(json.dumps(ydl.sanitize_info(info)))
"""


@contextmanager
def command(metadata=False):
    # This strong reference retains the old directory until the subprocess exits.
    with _LOCK:
        generation = _CURRENT
    code = _METADATA if metadata else "import runpy; runpy.run_module('yt_dlp', run_name='__main__')"
    yield [sys.executable, '-c', _BOOTSTRAP + code, generation.name if generation else '']


def extract_info(url, **options):
    settings = {'quiet': True, 'no_warnings': True, 'extract_flat': True,
                'skip_download': True, 'ignoreerrors': False, 'socket_timeout': 20, 'retries': 1}
    settings.update(options)
    with command(metadata=True) as executable:
        try:
            result = subprocess.run(executable, input=json.dumps({'url': url, 'options': settings}),
                                    capture_output=True, text=True, check=True, timeout=600)
            return json.loads(result.stdout)
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            raise RuntimeError('yt-dlp metadata lookup failed or timed out') from exc


def update():
    """Publish only a complete, importable installation. Never modify the fallback."""
    global _CURRENT
    candidate = None
    try:
        candidate = tempfile.TemporaryDirectory(prefix='pullarr-ytdlp-')
        subprocess.run(
            [sys.executable, '-m', 'pip', '--isolated', '--disable-pip-version-check',
             'install', '--index-url', 'https://pypi.org/simple', '--no-cache-dir',
             '--only-binary=:all:', '--timeout', '30', '--retries', '2',
             '--target', candidate.name, '--upgrade', 'yt-dlp[default]'],
            capture_output=True, text=True, check=True, timeout=600)
        result = subprocess.run(
            [sys.executable, '-c', _BOOTSTRAP +
             "from yt_dlp import YoutubeDL; from yt_dlp.version import __version__; print(__version__)",
             candidate.name], capture_output=True, text=True, check=True, timeout=60)
        with _LOCK:
            _CURRENT = candidate
        logging.info('yt-dlp update successful: %s', result.stdout.strip())
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not log pip output: local index/proxy credentials may appear there.
        logging.warning('yt-dlp update failed (%s); keeping working version; retry in one hour',
                        type(exc).__name__)
        if candidate is not None:
            candidate.cleanup()
        return False


def update_loop(stop):
    while not stop.is_set():
        started = time.monotonic()
        interval = UPDATE_INTERVAL if update() else RETRY_INTERVAL
        if stop.wait(max(0, interval - (time.monotonic() - started))):
            break

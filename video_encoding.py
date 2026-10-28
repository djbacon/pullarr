"""Sequential software encoding with source retention and atomic publication."""
import json
import logging
import math
import os
import shutil
import sqlite3
import subprocess
import tempfile
from fractions import Fraction
from collections.abc import Callable
from pathlib import Path

MODES = ('original', 'x264', 'x265')
SDR_FORMAT = 'bestvideo*[dynamic_range=SDR]+bestaudio/best[dynamic_range=SDR]'


def validate_mode(value):
    if not isinstance(value, str) or value not in MODES:
        raise ValueError('Video encoding must be original, x264, or x265')
    return value


def get_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key='video_encoding'").fetchone()
    return validate_mode(row[0]) if row else 'original'


def probe(path: Path) -> dict:
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error', '-threads', '2', '-select_streams', 'v:0',
            '-read_intervals', '%+#64', '-show_streams', '-show_frames', '-show_format',
            '-of', 'json', str(path),
        ], capture_output=True, text=True, timeout=60, check=True)
        data = json.loads(result.stdout)
        stream = data['streams'][0]
        stream['format_duration'] = data.get('format', {}).get('duration')
        stream['side_data_list'] = stream.get('side_data_list', []) + [
            side for frame in data.get('frames', []) for side in frame.get('side_data_list', [])]
        return stream
    except (ValueError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f'Cannot inspect video for encoding: {path.name}') from exc


def hdr_parameters(stream: dict) -> list[str]:
    params = []
    for side in stream.get('side_data_list', []):
        if side.get('side_data_type') == 'Mastering display metadata':
            def scaled(key, scale):
                return round(Fraction(side[key]) * scale)
            try:
                display = ''.join(f'{label}({scaled(color + "_x", 50000)},{scaled(color + "_y", 50000)})'
                                  for label, color in [('G', 'green'), ('B', 'blue'), ('R', 'red'), ('WP', 'white_point')])
                display += f'L({scaled("max_luminance", 10000)},{scaled("min_luminance", 10000)})'
                params.append('master-display=' + display)
            except (KeyError, ValueError, ZeroDivisionError) as exc:
                raise RuntimeError('Invalid source HDR mastering metadata') from exc
        elif side.get('side_data_type') == 'Content light level metadata':
            params.append(f'max-cll={int(side["max_content"])},{int(side["max_average"])}')
    return sorted(set(params))


def encoder_args(mode: str, stream: dict) -> list[str]:
    validate_mode(mode)
    transfer = stream.get('color_transfer')
    hdr = transfer in ('smpte2084', 'arib-std-b67')
    if mode == 'x264' and hdr:
        raise RuntimeError('x264 requires an SDR source; choose x265 to preserve HDR')
    args = ['-c:v', 'lib' + mode, '-preset', 'medium', '-crf', '20' if mode == 'x264' else '22',
            '-pix_fmt', 'yuv420p' if mode == 'x264' else 'yuv420p10le', '-threads', '2']
    for key, flag in [('color_primaries', '-color_primaries'), ('color_transfer', '-color_trc'),
                      ('color_space', '-colorspace'), ('color_range', '-color_range')]:
        if stream.get(key) and stream[key] != 'unknown':
            args += [flag, stream[key]]
    if mode == 'x265':
        params = ['pools=2', 'frame-threads=2'] + hdr_parameters(stream)
        for key, option in [('color_primaries', 'colorprim'), ('color_transfer', 'transfer'), ('color_space', 'colormatrix')]:
            if stream.get(key) and stream[key] != 'unknown':
                params.append(option + '=' + stream[key])
        if transfer == 'smpte2084':
            params.append('hdr10=1')
        args += ['-x265-params', ':'.join(params)]
    return args


def conversion_priority() -> list[str]:
    """Lower only the encoder's priority, without changing the threaded app."""
    prefix = []
    for utility, options in [('nice', ['-n', '19']), ('ionice', ['-c', '3'])]:
        executable = shutil.which(utility)
        if executable:
            prefix += [executable, *options]
        else:
            logging.warning('Video conversion priority utility %s is unavailable', utility)
    return prefix


def source_duration(stream: dict) -> float | None:
    for key in ('duration', 'format_duration'):
        try:
            duration = float(stream.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    return None


def encoding_percent(line: str, duration: float | None) -> float | None:
    if duration is None or not line.startswith('out_time_us='):
        return None
    try:
        seconds = int(line.partition('=')[2].strip()) / 1_000_000
    except (ValueError, OverflowError):
        return None
    # Reserve 100% for validated output published at the final destination.
    return min(99.9, max(0.0, seconds / duration * 100))


def encode(source: Path, destination: Path, mode: str,
           progress: Callable[[float | None], None] | None = None) -> None:
    """Never replace existing media; keep staged input until validated output is published."""
    stream = probe(source)
    args = encoder_args(mode, stream)
    duration = source_duration(stream)
    if progress is not None:
        progress(0.0 if duration is not None else None)
        if duration is None:
            logging.warning('Video duration unavailable for %s; conversion percentage cannot be estimated', source.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.pullarr-encode-', dir=destination.parent) as work:
        output = Path(work) / 'video.mkv'
        # A file-backed log bounds memory even for long encodes.
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen([
                *conversion_priority(), 'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
                '-progress', 'pipe:1', '-nostats', '-threads', '2', '-i', str(source),
                '-map', '0:v:0', '-map', '0:a?', '-map_metadata', '0', *args,
                '-c:a', 'aac', '-b:a', '192k', str(output),
            ], stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8', errors='replace', bufsize=1) as process:
                for line in process.stdout:
                    percent = encoding_percent(line, duration)
                    if progress is not None and percent is not None:
                        progress(percent)
                returncode = process.wait()
            if returncode:
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 4000))
                raise RuntimeError('Video conversion failed: ' + errors.read().decode(errors='replace'))
        encoded = probe(output)
        expected = 'h264' if mode == 'x264' else 'hevc'
        if not output.stat().st_size or encoded.get('codec_name') != expected:
            raise RuntimeError('Encoded video failed codec validation')
        for key in ('width', 'height', 'color_transfer', 'color_primaries', 'color_space'):
            if stream.get(key) not in (None, 'unknown') and encoded.get(key) != stream[key]:
                raise RuntimeError(f'Encoded video did not preserve {key}')
        if mode == 'x265' and hdr_parameters(stream) != hdr_parameters(encoded):
            raise RuntimeError('Encoded video did not preserve static HDR metadata')
        os.replace(output, destination)


def ready_path(source: Path, mode: str) -> Path:
    return source.with_name(source.name + '.' + mode + '.ready.mkv')


def convert(source: Path, destination: Path, mode: str,
            progress: Callable[[float | None], None] | None = None) -> Path:
    ready = ready_path(source, mode)
    if destination.exists() and not (ready.exists() and os.path.samefile(ready, destination)):
        raise RuntimeError(f'Output already exists; refusing to replace {destination.name}')
    if not ready.exists():
        encode(source, ready, mode, progress)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        os.link(ready, destination)
    artwork = source.with_suffix('.jpg')
    if artwork.is_file():
        try:
            os.link(artwork, destination.with_suffix('.jpg'))
        except FileExistsError:
            pass
    if progress is not None:
        progress(100.0)
    return destination


def cleanup(source: Path, mode: str) -> None:
    # Called only after the database commit: the ready hard link supports crash recovery.
    for path in (source, ready_path(source, mode), source.with_suffix('.jpg')):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logging.warning('Could not remove conversion staging file %s: %s', path, exc)

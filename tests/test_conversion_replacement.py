"""Keep published originals until a confirmed conversion can replace them safely."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import job_control
import video_encoding


class ConversionReplacementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db_path = self.root / 'state.db'
        self.db = app.init_db(self.db_path)
        self.addCleanup(lambda: self.db.close())
        self.config = app.AppConfig(download_root=self.root / 'downloads')
        self.entry = app.VideoEntry('abcdefghijk', 'UC' + 'A' * 22, 'Channel', 'Episode',
                                    '2026-11-19T08:00:00Z', 'https://www.youtube.com/watch?v=abcdefghijk')
        app.queue_entry(self.db, self.entry, True)
        self.db.execute("INSERT INTO settings VALUES ('video_encoding','x265')")
        self.db.commit()
        staging = self.config.download_root / '.pullarr-partial/x265' / self.entry.video_id
        self.source = Path(app.build_output_template(staging, self.entry).replace('%(ext)s', 'mkv'))
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b'original')
        self.source.with_suffix('.jpg').write_bytes(b'artwork')
        self.original = self.config.download_root / self.source.relative_to(staging)
        self.output = self.original.with_suffix('.mkv')
        self.job = app.DownloadedVideo(self.entry, self.config.download_root, 'x265', self.source)
        app.save_download_job(self.db, self.job)
        for name in ('is_short', 'stream_download'):
            mock = patch.object(app, name, side_effect=AssertionError('Unexpected YouTube request'))
            mock.start()
            self.addCleanup(mock.stop)

    def encode(self, source, ready, mode, progress=None, cancel=None):
        self.assertEqual(self.original.read_bytes(), b'original')
        self.assertTrue(self.original.with_suffix('.jpg').is_file())
        ready.write_bytes(b'encoded')

    def test_same_name_original_is_visible_and_atomic_replacement_retains_recovery_link(self):
        app.publish_download(self.job)
        self.assertTrue(os.path.samefile(self.source, self.original))
        with patch.object(video_encoding, 'encode', side_effect=self.encode):
            self.assertEqual(app.convert_download(self.job), self.output)
        self.assertEqual(self.original.read_bytes(), b'encoded')
        self.assertEqual(self.source.read_bytes(), b'original')
        ready = video_encoding.ready_path(self.source, 'x265')
        self.assertTrue(os.path.samefile(ready, self.output))
        # Simulate a restart after atomic replacement but before the history commit.
        self.db.close()
        self.db = app.init_db(self.db_path)
        with patch.object(video_encoding, 'encode') as encode:
            self.assertTrue(app.download_video(self.config, self.db, self.entry))
        encode.assert_not_called()
        self.assertEqual(self.db.execute('SELECT status,file_path FROM videos').fetchone(),
                         ('downloaded', str(self.output)))
        self.assertFalse(self.source.exists())
        self.assertFalse(ready.exists())
        self.assertEqual(self.output.read_bytes(), b'encoded')

    def test_conversion_failure_preserves_the_visible_original_for_local_retry(self):
        with patch.object(video_encoding, 'encode', side_effect=RuntimeError('encoder failed')), \
             self.assertLogs(level='ERROR'):
            self.assertFalse(app.download_video(self.config, self.db, self.entry))
        self.assertEqual(self.original.read_bytes(), b'original')
        self.assertTrue(os.path.samefile(self.source, self.original))
        self.assertEqual(self.db.execute('SELECT status FROM videos').fetchone()[0], 'failed')
        with patch.object(video_encoding, 'encode', side_effect=self.encode):
            self.assertTrue(app.download_video(self.config, self.db, self.entry))
        self.assertEqual(self.output.read_bytes(), b'encoded')

    def test_different_container_removes_only_the_replaced_original(self):
        source = self.source.with_suffix('.webm')
        self.source.rename(source)
        self.source = source
        self.original = self.original.with_suffix('.webm')
        self.job = app.DownloadedVideo(self.entry, self.config.download_root, 'x265', source)
        app.save_download_job(self.db, self.job)
        with patch.object(video_encoding, 'encode', side_effect=self.encode):
            self.assertTrue(app.download_video(self.config, self.db, self.entry))
        self.assertFalse(self.original.exists())
        self.assertEqual(self.output.read_bytes(), b'encoded')
        self.assertEqual(self.output.with_suffix('.jpg').read_bytes(), b'artwork')

    def test_existing_media_collision_is_preserved_without_encoding(self):
        self.original.parent.mkdir(parents=True)
        self.original.write_bytes(b'unrelated')
        with patch.object(video_encoding, 'encode') as encode, self.assertLogs(level='ERROR'):
            self.assertFalse(app.download_video(self.config, self.db, self.entry))
        encode.assert_not_called()
        self.assertEqual(self.original.read_bytes(), b'unrelated')
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_saved_job_through_download_root_symlink_completes_and_cleans_staging(self):
        alias = self.root / 'download-alias'
        alias.symlink_to(self.config.download_root, target_is_directory=True)
        source = alias / self.source.relative_to(self.config.download_root)
        job = app.DownloadedVideo(self.entry, alias, 'x265', source)
        app.save_download_job(self.db, job)
        with patch.object(video_encoding, 'encode', side_effect=self.encode):
            self.assertTrue(app.download_video(self.config, self.db, self.entry))
        self.assertFalse(self.source.exists())
        self.assertEqual(self.output.read_bytes(), b'encoded')

    def test_file_changed_during_encoding_is_not_overwritten(self):
        app.publish_download(self.job)
        def encode(source, ready, mode, progress=None, cancel=None):
            self.encode(source, ready, mode, progress, cancel)
            self.original.unlink()
            self.original.write_bytes(b'unrelated')
        with patch.object(video_encoding, 'encode', side_effect=encode), self.assertLogs(level='ERROR'):
            self.assertFalse(app.download_video(self.config, self.db, self.entry))
        self.assertEqual(self.original.read_bytes(), b'unrelated')
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_cancellation_before_replacement_keeps_original(self):
        app.publish_download(self.job)
        def encode(source, ready, mode, progress=None, cancel=None):
            self.encode(source, ready, mode, progress, cancel)
            cancel.cancel()
        with patch.object(video_encoding, 'encode', side_effect=encode):
            with self.assertRaises(job_control.Cancelled):
                app.convert_download(self.job)
        self.assertEqual(self.original.read_bytes(), b'original')

    def test_symlinked_destination_outside_root_is_rejected(self):
        self.original.parent.parent.mkdir(parents=True)
        outside = self.root / 'outside'
        outside.mkdir()
        self.original.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            app.publish_download(self.job)
        self.assertEqual(list(outside.iterdir()), [])

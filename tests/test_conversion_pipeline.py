import os
import tempfile
import threading
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

import app


class ConversionPipelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = app.init_db(self.root / 'state.db')
        self.addCleanup(lambda: self.db.close())
        self.config = app.AppConfig(download_root=self.root / 'downloads')
        self.cid = 'UC' + 'A' * 22
        self.db.execute('''INSERT INTO subscriptions(channel_id,title,added_at,mode_since,download_mode)
            VALUES (?,?,?,?,'all')''', (self.cid, 'Channel', app.utc_now(), '2020-01-01T00:00:00Z'))
        self.entries = [app.VideoEntry(f'video{n}', self.cid, 'Channel', f'Episode {n}',
                                      f'2026-10-{n:02d}T00:00:00Z', f'https://www.youtube.com/watch?v=video{n}')
                        for n in range(1, 4)]
        for entry in self.entries:
            app.queue_entry(self.db, entry, True)
        self.db.execute("INSERT INTO settings(key,value) VALUES ('video_encoding','x265')")
        self.db.commit()
        self.downloads = []
        self.conversions = []
        self.sources = {}
        self.outputs = {}
        self.real_convert = app.video_encoding.convert
        for name, replacement in (('DOWNLOAD_PROGRESS', app.DownloadProgress()),
                                  ('CONVERSION_PROGRESS', app.DownloadProgress()),
                                  ('ACTIVE_JOBS', {}), ('STOP', False)):
            mock = patch.object(app, name, replacement)
            mock.start()
            self.addCleanup(mock.stop)
        for target, attribute, kwargs in (
            (app, 'stream_download', {'side_effect': self.download}),
            (app.video_encoding, 'convert', {'side_effect': self.convert}),
            (app, 'is_short', {'return_value': False}),
            (app, 'discover_channel', {}), (app.channel_assets, 'sync', {}),
        ):
            mock = patch.object(target, attribute, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

    def download(self, command, cancel=None):
        video_id = command[-1].split('=')[-1]
        self.downloads.append(video_id)
        entry = next(entry for entry in self.entries if entry.video_id == video_id)
        root = Path(command[command.index('--paths') + 1].removeprefix('home:'))
        source = Path(app.build_output_template(root, entry).replace('%(ext)s', 'mkv'))
        source.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            source.write_bytes(video_id.encode())
        source.with_suffix('.jpg').write_bytes(b'artwork')
        self.sources[video_id] = source
        return str(source)

    def convert(self, source, destination, mode, progress=None, cancel=None):
        self.conversions.append((source, destination, mode))
        def encode(source, ready, mode, progress=None, cancel=None):
            ready.write_bytes(b'encoded ' + source.read_bytes())
        with patch.object(app.video_encoding, 'encode', side_effect=encode):
            self.real_convert(source, destination, mode, progress, cancel)
        self.outputs[source.read_text()] = destination
        return destination

    def statuses(self):
        return dict(self.db.execute('SELECT video_id,status FROM videos'))

    def test_all_downloads_continue_and_encoders_advance_during_a_blocked_download(self):
        encoding = threading.Event()
        next_downloaded = threading.Event()
        release = threading.Event()
        second_completed = threading.Event()
        main_thread = threading.get_ident()
        complete = app.complete_download
        cleanup = app.cleanup_idle_downloads

        def convert(source, destination, mode, progress=None, cancel=None):
            if source == self.sources['video1']:
                encoding.set()
                if not release.wait(5):
                    raise RuntimeError('Timed out waiting for overlapping download')
            return self.convert(source, destination, mode, progress)

        def download(command, cancel=None):
            if command[-1].endswith('video2'):
                self.assertTrue(encoding.wait(5))
                self.assertEqual(app.CONVERSION_PROGRESS.snapshot()['video_id'], 'video1')
                self.assertEqual(app.DOWNLOAD_PROGRESS.snapshot()['video_id'], 'video2')
                states = {row['video_id']: row['status'] for row in app.download_queue(self.db)}
                self.assertEqual(states, {'video1': 'converting', 'video2': 'downloading', 'video3': 'queued'})
                self.assertEqual(len(app.download_queue(self.db, True)), 3)
                profile = {row['video_id']: row['status'] for row in app.channel_profile(self.db, self.cid)['videos']}
                self.assertEqual(profile, states)
                with self.assertRaisesRegex(ValueError, 'still downloading'):
                    app.remove_download(self.db, 'video1')
                app.cleanup_idle_downloads(self.config, self.db)
                self.assertTrue(self.sources['video1'].exists())
                result = self.download(command)
                next_downloaded.set()
                return result
            if command[-1].endswith('video3'):
                self.assertFalse(release.is_set())
                self.assertEqual(len(self.conversions), 0)
                # The second original is already in its final folder while the first
                # conversion is blocked. A third download must still start.
                original = self.config.download_root / self.sources['video2'].relative_to(
                    self.config.download_root / '.pullarr-partial/x265/video2')
                self.assertEqual(original.read_bytes(), b'video2')
                self.assertTrue(os.path.samefile(original, self.sources['video2']))
                release.set()
                self.assertTrue(second_completed.wait(5), 'Encoder waited for the third download')
                self.assertEqual(self.statuses()['video1'], 'downloaded')
                self.assertEqual(self.statuses()['video2'], 'downloaded')
                self.assertEqual(original.read_bytes(), b'encoded video2')
            return self.download(command)

        def publish(conn, job, path):
            self.assertNotEqual(threading.get_ident(), main_thread)
            self.assertTrue(Path(path).is_file())
            complete(conn, job, path)
            if job.entry.video_id == 'video2':
                second_completed.set()

        def clean(config, conn):
            if next_downloaded.is_set() and not app.active_downloads():
                self.assertEqual(set(self.statuses().values()), {'downloaded'})
                self.assertEqual(len(self.outputs), 3)
            return cleanup(config, conn)

        try:
            with patch.object(app, 'stream_download', side_effect=download), \
                 patch.object(app.video_encoding, 'convert', side_effect=convert), \
                 patch.object(app, 'complete_download', side_effect=publish), \
                 patch.object(app, 'cleanup_idle_downloads', side_effect=clean):
                app.run_once(self.config, self.db)
        finally:
            release.set()
        self.assertEqual(self.downloads, ['video1', 'video2', 'video3'])
        self.assertEqual(len(self.conversions), 3)
        self.assertEqual(set(self.statuses().values()), {'downloaded'})
        self.assertFalse((self.config.download_root / '.pullarr-partial').exists())
        self.assertEqual(app.active_downloads(), [])

    def test_conversion_failure_preserves_source_and_next_job_succeeds_then_retries_after_restart(self):
        def convert(source, destination, mode, progress=None, cancel=None):
            if source == self.sources['video1']:
                raise RuntimeError('encoder failed')
            return self.convert(source, destination, mode, progress)
        with patch.object(app.video_encoding, 'convert', side_effect=convert), self.assertLogs(level='ERROR'):
            app.run_once(self.config, self.db)
        self.assertEqual(self.statuses(), {'video1': 'failed', 'video2': 'downloaded', 'video3': 'downloaded'})
        self.assertTrue(self.sources['video1'].exists())
        self.assertEqual(app.active_downloads(), [])
        self.db.close()
        self.db = app.init_db(self.root / 'state.db')
        app.run_once(self.config, self.db)
        self.assertEqual(self.downloads, ['video1', 'video2', 'video3'])
        self.assertEqual(set(self.statuses().values()), {'downloaded'})
        self.assertFalse((self.config.download_root / '.pullarr-partial').exists())

    def test_download_failure_does_not_discard_prior_conversion(self):
        started = threading.Event()
        release = threading.Event()
        def convert(source, destination, mode, progress=None, cancel=None):
            if source == self.sources['video1']:
                started.set()
                if not release.wait(5):
                    raise RuntimeError('Timed out')
            return self.convert(source, destination, mode, progress)
        def download(command, cancel=None):
            if command[-1].endswith('video2'):
                self.assertTrue(started.wait(5))
                release.set()
                raise OSError('network failure')
            return self.download(command)
        with patch.object(app, 'stream_download', side_effect=download), \
             patch.object(app.video_encoding, 'convert', side_effect=convert), self.assertLogs(level='ERROR'):
            try:
                app.run_once(self.config, self.db)
            finally:
                release.set()
        self.assertEqual(self.statuses(), {'video1': 'downloaded', 'video2': 'failed', 'video3': 'downloaded'})
        self.assertTrue(self.outputs['video1'].exists())
        self.assertEqual(app.active_downloads(), [])

    def test_settings_changes_only_apply_to_next_download(self):
        new_root = self.root / 'new-downloads'
        def download(command, cancel=None):
            result = self.download(command)
            if command[-1].endswith('video1'):
                app.save_download_root(self.db, str(new_root))
                with self.db:
                    self.db.execute("UPDATE settings SET value='x264' WHERE key='video_encoding'")
            return result
        with patch.object(app, 'stream_download', side_effect=download):
            app.run_once(self.config, self.db)
        first, second, third = self.conversions
        self.assertEqual([job[2] for job in (first, second, third)], ['x265', 'x264', 'x264'])
        self.assertTrue(first[1].is_relative_to(self.config.download_root))
        self.assertTrue(second[1].is_relative_to(new_root))
        self.assertTrue(third[1].is_relative_to(new_root))
        self.assertEqual(set(self.statuses().values()), {'downloaded'})

    def test_shutdown_preserves_prefetched_source_and_starts_no_next_conversion(self):
        started = threading.Event()
        release = threading.Event()
        def convert(source, destination, mode, progress=None, cancel=None):
            started.set()
            self.assertTrue(release.wait(5))
            return self.convert(source, destination, mode, progress, cancel)
        def download(command, cancel=None):
            result = self.download(command)
            if command[-1].endswith('video2'):
                self.assertTrue(started.wait(5))
                app.STOP = True
                release.set()
            return result
        with patch.object(app, 'stream_download', side_effect=download), \
             patch.object(app.video_encoding, 'convert', side_effect=convert):
            try:
                app.run_once(self.config, self.db)
            finally:
                release.set()
        self.assertEqual(self.downloads, ['video1', 'video2'])
        self.assertEqual(len(self.conversions), 1)
        self.assertEqual(self.statuses(), {'video1': 'downloaded', 'video2': 'pending', 'video3': 'pending'})
        self.assertTrue(self.sources['video2'].exists())
        self.assertEqual(app.active_downloads(), [])

    def test_polling_continues_without_requeuing_pending_conversions(self):
        started = threading.Event()
        release = threading.Event()
        def convert(source, destination, mode, progress=None, cancel=None):
            started.set()
            self.assertTrue(release.wait(5))
            return self.convert(source, destination, mode, progress, cancel)
        with patch.object(app.video_encoding, 'convert', side_effect=convert), \
             app.ConversionPipeline(self.db) as pipeline:
            try:
                app.run_once(self.config, self.db, pipeline)
                self.assertTrue(started.wait(5))
                self.assertEqual(self.downloads, ['video1', 'video2', 'video3'])
                self.assertEqual(self.db.execute('SELECT count(*) FROM download_jobs').fetchone()[0], 3)
                app.run_once(self.config, self.db, pipeline)
                self.assertEqual(self.downloads, ['video1', 'video2', 'video3'])
                self.assertEqual(len(pipeline.futures), 3)
            finally:
                release.set()
        self.assertEqual(set(self.statuses().values()), {'downloaded'})

    def test_idle_cleanup_keeps_queued_conversion_inputs_after_subscription_removal(self):
        self.download(['--paths', 'home:' + str(self.config.download_root / '.pullarr-partial/x265/video1'),
                       self.entries[0].url])
        with self.db:
            self.db.execute('DELETE FROM subscriptions')
        app.ACTIVE_JOBS['video1'] = app.job_control.Cancellation()
        self.assertEqual(app.active_downloads(), [])
        app.cleanup_idle_downloads(self.config, self.db)
        self.assertTrue(self.sources['video1'].is_file())

    def assert_subscription_change_stops_next_download(self, action):
        def download(command, cancel=None):
            result = self.download(command)
            if command[-1].endswith('video2'):
                with self.db:
                    self.db.execute(action)
            return result
        with patch.object(app, 'stream_download', side_effect=download):
            app.run_once(self.config, self.db)
        self.assertEqual(self.downloads, ['video1', 'video2'])
        self.assertEqual(len(self.conversions), 2)
        self.assertEqual(self.statuses(), {'video1': 'downloaded', 'video2': 'downloaded', 'video3': 'pending'})
        self.assertTrue(all(path.exists() for path in self.outputs.values()))
        self.assertEqual(app.active_downloads(), [])

    def test_disabling_finishes_started_jobs_without_downloading_next(self):
        self.assert_subscription_change_stops_next_download('UPDATE subscriptions SET enabled=0')

    def test_removing_finishes_started_jobs_without_downloading_next(self):
        self.assert_subscription_change_stops_next_download('DELETE FROM subscriptions')

    def test_pipeline_overlaps_downloads_across_channels(self):
        other = 'UC' + 'B' * 22
        with self.db:
            self.db.execute("INSERT INTO subscriptions(channel_id,title,added_at,mode_since,download_mode) "
                            "VALUES (?,'Other',?,?,'all')", (other, app.utc_now(), '2020-01-01T00:00:00Z'))
            self.db.execute("UPDATE videos SET channel_id=?,channel_title='Other' WHERE video_id!='video1'", (other,))
        self.entries = [self.entries[0]] + [replace(entry, channel_id=other, channel_title='Other')
                                             for entry in self.entries[1:]]
        started = threading.Event()
        release = threading.Event()
        def convert(source, destination, mode, progress=None, cancel=None):
            if source == self.sources['video1']:
                started.set()
                if not release.wait(5):
                    raise RuntimeError('No next-channel download started')
            return self.convert(source, destination, mode, progress)
        def download(command, cancel=None):
            if command[-1].endswith('video2'):
                self.assertTrue(started.wait(5))
                self.assertEqual(app.CONVERSION_PROGRESS.snapshot()['channel_id'], self.cid)
                self.assertEqual(app.DOWNLOAD_PROGRESS.snapshot()['channel_id'], other)
                release.set()
            return self.download(command)
        with patch.object(app, 'stream_download', side_effect=download), \
             patch.object(app.video_encoding, 'convert', side_effect=convert):
            try:
                app.run_once(self.config, self.db)
            finally:
                release.set()
        self.assertEqual(set(self.statuses().values()), {'downloaded'})
        self.assertEqual(self.downloads, ['video1', 'video2', 'video3'])

    def test_original_mode_never_starts_encoder(self):
        with self.db:
            self.db.execute("UPDATE settings SET value='original' WHERE key='video_encoding'")
        with patch.object(app.video_encoding, 'convert') as convert:
            app.run_once(self.config, self.db)
        convert.assert_not_called()
        self.assertEqual(set(self.statuses().values()), {'downloaded'})
        self.assertEqual(self.downloads, ['video1', 'video2', 'video3'])

    def test_conversion_cannot_publish_outside_its_staging_directory(self):
        source = self.root / 'outside.mkv'
        source.write_bytes(b'keep')
        job = app.DownloadedVideo(self.entries[0], self.config.download_root, 'x265', source)
        with app.ConversionPipeline(self.db) as pipeline, self.assertLogs(level='ERROR'):
            self.assertTrue(pipeline.submit(job))
            pipeline.finish()
        self.assertEqual(self.statuses()['video1'], 'failed')
        self.assertEqual(source.read_bytes(), b'keep')
        self.assertEqual(self.conversions, [])

    def test_missing_converted_output_is_not_recorded_successfully(self):
        with patch.object(app.video_encoding, 'convert', return_value=self.root / 'missing.mkv'), \
             self.assertLogs(level='ERROR'):
            app.run_once(self.config, self.db)
        self.assertEqual(set(self.statuses().values()), {'failed'})
        self.assertTrue(all(source.exists() for source in self.sources.values()))
        self.assertEqual(app.active_downloads(), [])

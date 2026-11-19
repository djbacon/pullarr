"""Cancel one media job, including the subprocesses started by yt-dlp or ffmpeg."""
from contextlib import contextmanager
import logging
import os
import signal
import subprocess
import threading


class Cancelled(Exception):
    """The user canceled this job; it must not be retried or marked complete."""


class Cancellation:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.processes = {}

    def check(self):
        if self.event.is_set():
            raise Cancelled()

    def _signal(self, process, force=False):
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif force:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            pass  # The process group already exited.
        except OSError as exc:
            logging.error('Could not stop media process %s: %s', process.pid, exc)

    def _kill(self, process):
        with self.lock:
            if process in self.processes:
                self._signal(process, force=True)

    def cancel(self):
        with self.lock:
            self.event.set()
            for process, timer in self.processes.items():
                if timer is not None:
                    continue
                self._signal(process)
                timer = threading.Timer(2, self._kill, args=(process,))
                timer.daemon = True
                self.processes[process] = timer
                timer.start()

    @contextmanager
    def process(self, command, **kwargs):
        with self.lock:
            self.check()
            process = subprocess.Popen(command, start_new_session=os.name == 'posix', **kwargs)
            self.processes[process] = None
        try:
            with process:
                yield process
        finally:
            with self.lock:
                timer = self.processes.pop(process)
                if timer is not None:
                    timer.cancel()
                    self._signal(process, force=True)
            self.check()

    def run(self, command, timeout):
        with self.process(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._signal(process, force=True)
                process.communicate()
                raise
            self.check()
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

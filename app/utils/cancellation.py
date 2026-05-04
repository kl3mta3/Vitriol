"""Cooperative cancellation token shared by subprocess and pure-Python handlers."""
from __future__ import annotations
import threading


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._on_cancel: list = []

    def cancel(self) -> None:
        self._event.set()
        for cb in list(self._on_cancel):
            try:
                cb()
            except Exception:
                pass

    def is_set(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise CancelledError if cancellation was requested."""
        if self._event.is_set():
            raise CancelledError()

    def on_cancel(self, callback) -> None:
        """Register a callback (e.g. Popen.terminate) invoked when cancel() fires."""
        self._on_cancel.append(callback)
        if self._event.is_set():
            try:
                callback()
            except Exception:
                pass


class CancelledError(Exception):
    pass

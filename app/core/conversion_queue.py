"""Background conversion queue. QThreadPool with at most 3 concurrent jobs.

Each job carries a CancellationToken. Cancellation is honored either by
subprocess termination (via on_cancel callbacks the handler registers) or by
cooperative checks in pure-Python handlers.
"""
from __future__ import annotations
import atexit
import hashlib
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QRunnable, Signal, QThreadPool, Slot

from .router import convert_file, UnsupportedConversionError
from .config import PROGRESS_EMIT_THROTTLE_MS
from ..utils.cancellation import CancellationToken, CancelledError
from ..utils.logger import get_logger
from ..utils.paths import unique_path

_log = get_logger()


class JobSignals(QObject):
    started = Signal(int)                  # job_id
    progress = Signal(int, float)          # job_id, 0..1
    elapsed = Signal(int, float)           # job_id, seconds
    finished = Signal(int, str)            # job_id, final output path
    failed = Signal(int, str)              # job_id, error message
    cancelled = Signal(int)                # job_id
    warning = Signal(int, str)             # job_id, human-readable warning
    bytes_progress = Signal(int, int, int) # job_id, processed_bytes, total_bytes


@dataclass
class Job:
    id: int
    src: Path
    dst: Path
    src_ext: str
    dst_ext: str
    save_over_original: bool
    masquerade: bool = False
    verify_round_trip: bool = False
    compiler: bool = False  # .py target: produce a self-extracting Stone script
    password: bytes = b""   # Stone v3 envelope encryption key; empty = default
    preserve_animations: bool = False  # 3D: keep rigs/animations through Assimp
    total_bytes: int = 0   # populated at submit() from src.stat().st_size
    cancel: CancellationToken = field(default_factory=CancellationToken)


class _Runnable(QRunnable):
    def __init__(self, job: Job, signals: JobSignals) -> None:
        super().__init__()
        self.job = job
        self.signals = signals
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        job = self.job
        sig = self.signals
        sig.started.emit(job.id)
        start = time.monotonic()
        # Throttle bytes_progress to one emit per PROGRESS_EMIT_THROTTLE_MS
        # so giant streaming conversions don't spam the UI thread.
        last_bytes_emit = [0.0]

        def emit_elapsed() -> None:
            sig.elapsed.emit(job.id, time.monotonic() - start)

        # Background ticker: emits elapsed every 250 ms so the per-row
        # timer keeps ticking even when the conversion code itself isn't
        # reporting progress (e.g. the multi-second Mandelbrot keystream
        # generation step). Stops as soon as run() exits.
        ticker_stop = threading.Event()

        def _ticker():
            while not ticker_stop.wait(0.25):
                try:
                    emit_elapsed()
                except Exception:
                    return

        ticker_thread = threading.Thread(target=_ticker, daemon=True,
                                          name=f"vitriol-ticker-{job.id}")
        ticker_thread.start()

        def emit_bytes(p: float) -> None:
            if job.total_bytes <= 0:
                return
            now = time.monotonic()
            # Always emit at p==1.0 (final) so the UI lands on the exact total.
            if p >= 0.999 or (now - last_bytes_emit[0]) * 1000 >= PROGRESS_EMIT_THROTTLE_MS:
                last_bytes_emit[0] = now
                processed = int(min(1.0, max(0.0, p)) * job.total_bytes)
                sig.bytes_progress.emit(job.id, processed, job.total_bytes)

        def on_progress(p: float) -> None:
            emit_elapsed()
            # When verifying, forward progress is shown as the first half.
            if job.verify_round_trip:
                shown = p * 0.5
            else:
                shown = p
            sig.progress.emit(job.id, shown)
            emit_bytes(shown)

        dst = job.dst if job.save_over_original else unique_path(job.dst)
        warnings: list[str] = []

        try:
            if job.verify_round_trip:
                # Forward into a temp file, then reverse into another temp,
                # compare hashes, only commit on success.
                self._run_with_verify(job, sig, dst, warnings, on_progress, emit_elapsed)
            else:
                convert_file(job.src, dst, job.src_ext, job.dst_ext, job.cancel,
                             on_progress, warnings,
                             masquerade=job.masquerade, compiler=job.compiler,
                             password=job.password,
                             preserve_animations=job.preserve_animations)
                for w in warnings:
                    sig.warning.emit(job.id, w)
                if job.save_over_original and dst != job.src:
                    try:
                        if job.src.exists() and job.src.resolve() != dst.resolve():
                            job.src.unlink()
                    except OSError as e:
                        _log.warning("could not delete original %s: %s", job.src, e)
                emit_elapsed()
                sig.finished.emit(job.id, str(dst))
        except CancelledError:
            _log.info("job %d cancelled", job.id)
            try:
                if dst.exists() and not job.save_over_original:
                    dst.unlink()
            except OSError:
                pass
            sig.cancelled.emit(job.id)
        except UnsupportedConversionError as e:
            _log.warning("unsupported conversion for job %d: %s", job.id, e)
            sig.failed.emit(job.id, str(e))
        except Exception as e:
            _log.exception("job %d failed", job.id)
            sig.failed.emit(job.id, f"{type(e).__name__}: {e}")
        finally:
            ticker_stop.set()
            ticker_thread.join(timeout=1.0)

    def _run_with_verify(self, job: Job, sig: "JobSignals", final_dst: Path,
                          warnings: list, on_progress, emit_elapsed) -> None:
        """Forward into temp/forward, reverse into temp/reverse, hash-compare
        against source. Commit on byte-exact match, fail with a clear tooltip
        otherwise. Distinguish 'verification could not complete' (I/O error
        during compare — let user retry) from 'bytes differ'."""
        tmp_dir = Path(tempfile.mkdtemp(prefix="uc_verify_"))
        _track_tempdir(tmp_dir)
        try:
            tmp_forward = tmp_dir / ("forward" + job.dst_ext)
            tmp_reverse = tmp_dir / ("reverse" + job.src_ext)

            convert_file(job.src, tmp_forward, job.src_ext, job.dst_ext,
                         job.cancel, on_progress, warnings,
                         masquerade=job.masquerade, compiler=job.compiler,
                         password=job.password)
            for w in warnings:
                sig.warning.emit(job.id, w)
            sig.progress.emit(job.id, 0.55)

            # Reverse direction (50% → 95% of progress band)
            def rev_progress(p: float) -> None:
                emit_elapsed()
                shown = 0.5 + p * 0.45
                sig.progress.emit(job.id, shown)
                # Inline byte-progress emit (same throttle window as forward).
                if job.total_bytes > 0:
                    now = time.monotonic()
                    if shown >= 0.95 or (now - last_bytes_emit[0]) * 1000 >= PROGRESS_EMIT_THROTTLE_MS:
                        last_bytes_emit[0] = now
                        processed = int(min(1.0, max(0.0, shown)) * job.total_bytes)
                        sig.bytes_progress.emit(job.id, processed, job.total_bytes)

            rev_warnings: list[str] = []
            convert_file(tmp_forward, tmp_reverse, job.dst_ext, job.src_ext,
                         job.cancel, rev_progress, rev_warnings,
                         masquerade=job.masquerade, compiler=job.compiler,
                         password=job.password)
            sig.progress.emit(job.id, 0.95)

            try:
                src_hash = _sha256_file(job.src)
                rev_hash = _sha256_file(tmp_reverse)
            except OSError as e:
                sig.failed.emit(
                    job.id,
                    f"Verification could not complete (I/O error during hash compare): {e}. "
                    "The conversion itself may still be valid — try again or run without "
                    "Verify Round-Trip to keep the output."
                )
                return

            if src_hash != rev_hash:
                sig.failed.emit(
                    job.id,
                    "Verification failed — bytes differ. The forward+reverse round-trip "
                    "did not reproduce the original file. The conversion is not lossless "
                    "for this source/host combination."
                )
                return

            # Success — commit the forward output
            shutil.copy2(tmp_forward, final_dst)
            if job.save_over_original and final_dst != job.src:
                try:
                    if job.src.exists() and job.src.resolve() != final_dst.resolve():
                        job.src.unlink()
                except OSError as e:
                    _log.warning("could not delete original %s: %s", job.src, e)
            sig.progress.emit(job.id, 1.0)
            emit_elapsed()
            sig.finished.emit(job.id, str(final_dst))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            _untrack_tempdir(tmp_dir)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# Track verify-mode temp dirs so we can sweep them on app exit if a job died.
_TEMPDIRS: set[Path] = set()


def _track_tempdir(p: Path) -> None:
    _TEMPDIRS.add(p)


def _untrack_tempdir(p: Path) -> None:
    _TEMPDIRS.discard(p)


@atexit.register
def _cleanup_tempdirs() -> None:
    for p in list(_TEMPDIRS):
        shutil.rmtree(p, ignore_errors=True)
        _TEMPDIRS.discard(p)


class ConversionQueue(QObject):
    """Thin facade over QThreadPool. Hands out job ids, tracks cancel tokens."""

    job_started = Signal(int)
    job_progress = Signal(int, float)
    job_elapsed = Signal(int, float)
    job_finished = Signal(int, str)
    job_failed = Signal(int, str)
    job_cancelled = Signal(int)
    job_warning = Signal(int, str)
    job_bytes_progress = Signal(int, int, int)  # job_id, processed, total

    def __init__(self, max_workers: int = 3, parent=None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max_workers)
        self._next_id = 1
        self._tokens: dict[int, CancellationToken] = {}
        self._signals = JobSignals()
        self._signals.started.connect(self.job_started)
        self._signals.progress.connect(self.job_progress)
        self._signals.elapsed.connect(self.job_elapsed)
        self._signals.finished.connect(self._on_finished)
        self._signals.failed.connect(self._on_failed)
        self._signals.cancelled.connect(self._on_cancelled)
        self._signals.warning.connect(self.job_warning)
        self._signals.bytes_progress.connect(self.job_bytes_progress)

    def submit(
        self,
        src: Path,
        dst: Path,
        src_ext: str,
        dst_ext: str,
        save_over_original: bool = False,
        masquerade: bool = False,
        verify_round_trip: bool = False,
        compiler: bool = False,
        password: bytes = b"",
        preserve_animations: bool = False,
    ) -> int:
        job_id = self._next_id
        self._next_id += 1
        token = CancellationToken()
        self._tokens[job_id] = token
        try:
            total_bytes = src.stat().st_size
        except OSError:
            total_bytes = 0
        job = Job(
            id=job_id, src=src, dst=dst,
            src_ext=src_ext, dst_ext=dst_ext,
            save_over_original=save_over_original,
            masquerade=masquerade,
            verify_round_trip=verify_round_trip,
            compiler=compiler,
            password=password,
            preserve_animations=preserve_animations,
            total_bytes=total_bytes,
            cancel=token,
        )
        runnable = _Runnable(job, self._signals)
        self._pool.start(runnable)
        return job_id

    def cancel(self, job_id: int) -> None:
        token = self._tokens.get(job_id)
        if token:
            token.cancel()

    def cancel_all(self) -> None:
        for tok in self._tokens.values():
            tok.cancel()

    def _on_finished(self, job_id: int, path: str) -> None:
        self._tokens.pop(job_id, None)
        self.job_finished.emit(job_id, path)

    def _on_failed(self, job_id: int, msg: str) -> None:
        self._tokens.pop(job_id, None)
        self.job_failed.emit(job_id, msg)

    def _on_cancelled(self, job_id: int) -> None:
        self._tokens.pop(job_id, None)
        self.job_cancelled.emit(job_id)

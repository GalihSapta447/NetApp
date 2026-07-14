"""Pengelola streaming file video dan audio melalui UDP secara background."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import cv2

import udp_stream
from udp_audio import UDPAudioSender


class UDPVideoStreamer:
    def __init__(
        self,
        target_host: str,
        target_port: int,
        *,
        audio_target_port: int = 9003,
        audio_bitrate: str = "128k",
        jpeg_quality: int = 65,
        target_width: int = 640,
        fallback_fps: float = 20.0,
    ) -> None:
        self.target_host = target_host
        self.target_port = target_port
        self.jpeg_quality = max(20, min(95, jpeg_quality))
        self.target_width = max(240, target_width)
        self.fallback_fps = max(1.0, fallback_fps)
        self.audio_sender = UDPAudioSender(
            target_host,
            audio_target_port,
            bitrate=audio_bitrate,
        )

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._current_path: Path | None = None
        self._status = "idle"
        self._frames_sent = 0
        self._last_error = ""
        self._started_at: float | None = None
        self._source_fps: float | None = None
        self._duration_seconds: float | None = None
        self._position_seconds = 0.0

    def start(self, video_path: Path) -> None:
        video_path = video_path.resolve()
        if not video_path.is_file():
            raise FileNotFoundError(video_path)

        self.stop(wait=True)
        self._stop_event = threading.Event()
        with self._lock:
            self._current_path = video_path
            self._status = "starting"
            self._frames_sent = 0
            self._last_error = ""
            self._started_at = time.time()
            self._source_fps = None
            self._duration_seconds = None
            self._position_seconds = 0.0

        # Audio dan video berjalan sebagai sender UDP terpisah agar browser dapat
        # mengatur play/pause, mute, dan volume tanpa mengubah frame JPEG.
        self.audio_sender.start(video_path)
        self._thread = threading.Thread(
            target=self._run,
            args=(video_path, self._stop_event),
            daemon=True,
            name="udp-uploaded-video-streamer",
        )
        self._thread.start()

    def stop(self, wait: bool = False) -> None:
        thread = self._thread
        self._stop_event.set()
        self.audio_sender.stop(wait=wait)
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        with self._lock:
            if self._status not in {"idle", "error"}:
                self._status = "stopped"

    def restart(self) -> None:
        """Putar ulang file aktif dari awal dan buat sender UDP baru."""

        with self._lock:
            current_path = self._current_path

        if current_path is None:
            raise RuntimeError("Belum ada video aktif untuk dimuat ulang.")
        if not current_path.is_file():
            raise FileNotFoundError(current_path)

        self.start(current_path)

    def status(self) -> dict:
        with self._lock:
            current_name = self._current_path.name if self._current_path else None
            started_at = self._started_at
            status = {
                "state": self._status,
                "current_file": current_name,
                "frames_sent": self._frames_sent,
                "last_error": self._last_error,
                "target": f"{self.target_host}:{self.target_port}",
                "source_fps": round(self._source_fps, 2) if self._source_fps else None,
                "duration_seconds": (
                    round(self._duration_seconds, 2) if self._duration_seconds else None
                ),
                "position_seconds": round(self._position_seconds, 2),
                "started_seconds_ago": (
                    round(time.time() - started_at, 1) if started_at else None
                ),
                "audio": self.audio_sender.status(),
            }
        return status

    def _run(self, video_path: Path, stop_event: threading.Event) -> None:
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            with self._lock:
                self._status = "error"
                self._last_error = "File video tidak dapat dibuka oleh OpenCV."
            self.audio_sender.stop(wait=False)
            return

        source_fps = capture.get(cv2.CAP_PROP_FPS)
        fps = source_fps if source_fps and 1 <= source_fps <= 120 else self.fallback_fps
        total_frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        duration = total_frames / fps if total_frames and total_frames > 0 else None
        with self._lock:
            self._source_fps = fps
            self._duration_seconds = duration

        frame_delay = 1.0 / fps
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        frame_id = 0

        with self._lock:
            self._status = "streaming"

        try:
            while not stop_event.is_set():
                cycle_started = time.perf_counter()
                ok, frame = capture.read()

                # File selesai: ulangi dari awal agar stream tetap tersedia sampai dihentikan.
                if not ok:
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    with self._lock:
                        self._position_seconds = 0.0
                    time.sleep(0.05)
                    continue

                height, width = frame.shape[:2]
                if width > self.target_width:
                    new_height = max(1, int(height * self.target_width / width))
                    frame = cv2.resize(frame, (self.target_width, new_height))

                encoded_ok, encoded = cv2.imencode(".jpg", frame, encode_params)
                if not encoded_ok:
                    continue

                udp_stream.send_frame(
                    sock,
                    (self.target_host, self.target_port),
                    encoded.tobytes(),
                    frame_id,
                )
                frame_id = (frame_id + 1) & 0xFFFFFFFF

                # CAP_PROP_POS_FRAMES menunjuk ke frame berikutnya setelah read().
                # Dikurangi satu agar posisi pertama tampil sebagai 0:00.
                current_frame = max(0.0, capture.get(cv2.CAP_PROP_POS_FRAMES) - 1.0)
                position_seconds = current_frame / fps
                if duration is not None:
                    position_seconds = min(position_seconds, duration)

                with self._lock:
                    self._frames_sent += 1
                    self._position_seconds = position_seconds

                elapsed = time.perf_counter() - cycle_started
                stop_event.wait(max(0.0, frame_delay - elapsed))

        except Exception as error:
            with self._lock:
                self._status = "error"
                self._last_error = str(error)
        finally:
            capture.release()
            sock.close()
            self.audio_sender.stop(wait=False)
            with self._lock:
                if self._status != "error":
                    self._status = "stopped"
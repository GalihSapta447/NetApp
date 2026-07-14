"""Pengiriman dan penerimaan audio live menggunakan UDP.

Audio dari file video ditranskode menjadi MP3 secara real-time oleh FFmpeg,
dibagi menjadi datagram kecil, dikirim melalui UDP, lalu diteruskan ke browser
sebagai stream ``audio/mpeg``. Video tetap ditangani oleh ``udp_stream.py``.
"""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Iterator

import imageio_ffmpeg

MAGIC = b"AUD1"
HEADER = struct.Struct("!4sIB")  # magic, sequence, flags
FLAG_AUDIO = 0
FLAG_RESET = 1
MAX_AUDIO_PAYLOAD = 1200

# Buffer broadcast untuk beberapa browser sekaligus.
_BUFFER_LIMIT = 96
_condition = threading.Condition()
_packets: deque[tuple[int, bytes]] = deque(maxlen=_BUFFER_LIMIT)
_publish_id = 0
_generation = 0
_received_packets = 0
_received_bytes = 0
_estimated_lost_packets = 0
_last_sender_sequence: int | None = None
_last_packet_at = 0.0
_last_source = ""
_server_started = False
_server_address = ""


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _send_control_reset(sock: socket.socket, target: tuple[str, int]) -> None:
    packet = HEADER.pack(MAGIC, 0, FLAG_RESET)
    # Kirim dua kali agar reset lebih mungkin diterima pada jaringan UDP.
    sock.sendto(packet, target)
    time.sleep(0.01)
    sock.sendto(packet, target)


class UDPAudioSender:
    """Transkode track audio video dan kirim sebagai datagram UDP."""

    def __init__(self, target_host: str, target_port: int, bitrate: str = "128k") -> None:
        self.target_host = target_host
        self.target_port = target_port
        self.bitrate = bitrate

        self._lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._state = "idle"
        self._current_path: Path | None = None
        self._packets_sent = 0
        self._bytes_sent = 0
        self._last_error = ""
        self._started_at: float | None = None

    def start(self, video_path: Path) -> None:
        video_path = video_path.resolve()
        if not video_path.is_file():
            raise FileNotFoundError(video_path)

        self.stop(wait=True)
        self._stop_event = threading.Event()
        with self._lock:
            self._state = "starting"
            self._current_path = video_path
            self._packets_sent = 0
            self._bytes_sent = 0
            self._last_error = ""
            self._started_at = time.time()

        self._thread = threading.Thread(
            target=self._run,
            args=(video_path, self._stop_event),
            daemon=True,
            name="udp-audio-sender",
        )
        self._thread.start()

    def stop(self, wait: bool = False) -> None:
        self._stop_event.set()
        with self._process_lock:
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

        thread = self._thread
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
            if thread.is_alive() and process and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass

        with self._lock:
            if self._state not in {"idle", "error", "no_audio"}:
                self._state = "stopped"

    def status(self) -> dict:
        with self._lock:
            started_at = self._started_at
            return {
                "state": self._state,
                "current_file": self._current_path.name if self._current_path else None,
                "packets_sent": self._packets_sent,
                "bytes_sent": self._bytes_sent,
                "last_error": self._last_error,
                "target": f"{self.target_host}:{self.target_port}",
                "started_seconds_ago": (
                    round(time.time() - started_at, 1) if started_at else None
                ),
            }

    def _run(self, video_path: Path, stop_event: threading.Event) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        target = (self.target_host, self.target_port)
        process: subprocess.Popen[bytes] | None = None

        try:
            _send_control_reset(sock, target)
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            command = [
                ffmpeg_exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-stream_loop",
                "-1",
                "-i",
                str(video_path),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "2",
                "-ar",
                "44100",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                self.bitrate,
                "-f",
                "mp3",
                "pipe:1",
            ]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=_creation_flags(),
            )
            with self._process_lock:
                self._process = process
            with self._lock:
                self._state = "streaming"

            sequence = 0
            assert process.stdout is not None
            while not stop_event.is_set():
                payload = process.stdout.read(MAX_AUDIO_PAYLOAD)
                if not payload:
                    break
                packet = HEADER.pack(MAGIC, sequence, FLAG_AUDIO) + payload
                sock.sendto(packet, target)
                sequence = (sequence + 1) & 0xFFFFFFFF
                with self._lock:
                    self._packets_sent += 1
                    self._bytes_sent += len(payload)

            if not stop_event.is_set():
                return_code = process.wait(timeout=2.0)
                stderr = b""
                if process.stderr is not None:
                    stderr = process.stderr.read()
                error_text = stderr.decode("utf-8", errors="replace").strip()
                with self._lock:
                    if return_code != 0:
                        lowered = error_text.lower()
                        if (
                            "matches no streams" in lowered
                            or "does not contain any stream" in lowered
                            or "stream map '0:a:0' matches no streams" in lowered
                        ):
                            self._state = "no_audio"
                            self._last_error = "File video tidak memiliki track audio."
                        else:
                            self._state = "error"
                            self._last_error = error_text or f"FFmpeg berhenti dengan kode {return_code}."
                    else:
                        self._state = "stopped"

        except Exception as error:
            with self._lock:
                self._state = "error"
                self._last_error = str(error)
        finally:
            if process and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=1.0)
                except Exception:
                    try:
                        process.kill()
                    except OSError:
                        pass
            with self._process_lock:
                self._process = None
            sock.close()
            with self._lock:
                if self._state == "streaming":
                    self._state = "stopped"


def start_udp_audio_server(port: int = 9003, host: str = "0.0.0.0") -> None:
    """Jalankan receiver audio UDP dan publikasikan paket untuk browser."""

    global _server_started, _server_address
    global _publish_id, _generation, _received_packets, _received_bytes
    global _estimated_lost_packets, _last_sender_sequence
    global _last_packet_at, _last_source

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    _server_started = True
    _server_address = f"{host}:{port}"
    print(f"[UDP-AUDIO] Receiver aktif di {host}:{port}")

    while True:
        try:
            data, addr = sock.recvfrom(65535)
            if len(data) < HEADER.size:
                continue
            magic, sender_sequence, flags = HEADER.unpack(data[: HEADER.size])
            if magic != MAGIC:
                continue

            with _condition:
                if flags == FLAG_RESET:
                    _packets.clear()
                    _generation += 1
                    _last_sender_sequence = None
                    _last_packet_at = time.time()
                    _last_source = f"{addr[0]}:{addr[1]}"
                    _condition.notify_all()
                    continue

                if flags != FLAG_AUDIO:
                    continue
                payload = data[HEADER.size :]
                if not payload:
                    continue

                if _last_sender_sequence is not None:
                    expected = (_last_sender_sequence + 1) & 0xFFFFFFFF
                    if sender_sequence != expected:
                        gap = (sender_sequence - expected) & 0xFFFFFFFF
                        if 0 < gap < 10_000:
                            _estimated_lost_packets += gap
                _last_sender_sequence = sender_sequence

                _publish_id += 1
                _packets.append((_publish_id, payload))
                _received_packets += 1
                _received_bytes += len(payload)
                _last_packet_at = time.time()
                _last_source = f"{addr[0]}:{addr[1]}"
                _condition.notify_all()

        except OSError as error:
            print(f"[UDP-AUDIO] Socket error: {error}")
            time.sleep(0.2)
        except Exception as error:
            print(f"[UDP-AUDIO] Error menerima paket: {error}")


def get_audio_status(online_timeout: float = 3.0) -> dict:
    with _condition:
        age = time.time() - _last_packet_at if _last_packet_at else None
        return {
            "server_started": _server_started,
            "server_address": _server_address,
            "online": bool(age is not None and age <= online_timeout),
            "received_packets": _received_packets,
            "received_bytes": _received_bytes,
            "estimated_lost_packets": _estimated_lost_packets,
            "last_packet_age": round(age, 2) if age is not None else None,
            "source": _last_source,
            "generation": _generation,
        }


def audio_stream_generator() -> Iterator[bytes]:
    """Broadcast paket MP3 terbaru ke satu koneksi browser."""

    last_publish_id: int | None = None
    seen_generation: int | None = None

    while True:
        chunks: list[tuple[int, bytes]] = []
        with _condition:
            _condition.wait_for(
                lambda: bool(_packets)
                and (
                    seen_generation != _generation
                    or last_publish_id is None
                    or _packets[-1][0] > last_publish_id
                ),
                timeout=1.0,
            )

            if not _packets:
                continue

            if seen_generation != _generation or last_publish_id is None:
                # Mulai dekat posisi live agar suara tidak tertinggal jauh.
                chunks = list(_packets)[-6:]
                seen_generation = _generation
            else:
                chunks = [item for item in _packets if item[0] > last_publish_id]

            if chunks:
                last_publish_id = chunks[-1][0]

        for _, payload in chunks:
            yield payload

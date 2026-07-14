"""Utilitas penerima dan pengirim frame video melalui UDP.

Frame JPEG dapat lebih besar dari satu datagram. Modul ini membaginya menjadi
beberapa paket kecil, lalu menyusunnya kembali di sisi server.
"""

from __future__ import annotations

import math
import socket
import struct
import threading
import time
from typing import Dict, Iterator, Tuple

MAGIC = b"PJAR"
HEADER = struct.Struct("!4sIHH")
MAX_DATAGRAM = 60_000
CHUNK_PAYLOAD = MAX_DATAGRAM - HEADER.size
ASSEMBLY_TIMEOUT_SECONDS = 2.0

_latest_frame: bytes | None = None
_latest_frame_at = 0.0
_latest_sender = "-"
_received_frames = 0
_lock = threading.Lock()


def packetize_frame(frame: bytes, frame_id: int) -> Iterator[bytes]:
    """Pecah satu frame JPEG menjadi paket-paket UDP ber-header."""
    if not frame:
        return

    total_chunks = max(1, math.ceil(len(frame) / CHUNK_PAYLOAD))
    if total_chunks > 65_535:
        raise ValueError("Frame terlalu besar untuk dikirim melalui protokol UDP ini")

    for chunk_index in range(total_chunks):
        start = chunk_index * CHUNK_PAYLOAD
        payload = frame[start : start + CHUNK_PAYLOAD]
        yield HEADER.pack(
            MAGIC,
            frame_id & 0xFFFFFFFF,
            chunk_index,
            total_chunks,
        ) + payload


def send_frame(sock: socket.socket, address: Tuple[str, int], frame: bytes, frame_id: int) -> None:
    """Kirim satu frame JPEG melalui socket UDP yang sudah tersedia."""
    for packet in packetize_frame(frame, frame_id):
        sock.sendto(packet, address)


def get_latest_frame() -> bytes | None:
    with _lock:
        return _latest_frame


def get_stream_status() -> dict:
    with _lock:
        age = time.time() - _latest_frame_at if _latest_frame_at else None
        return {
            "online": bool(age is not None and age < 3.0),
            "last_frame_age": round(age, 2) if age is not None else None,
            "sender": _latest_sender,
            "received_frames": _received_frames,
        }


def _publish_frame(frame: bytes, sender: str) -> None:
    global _latest_frame, _latest_frame_at, _latest_sender, _received_frames

    # Hanya publikasikan data yang terlihat seperti JPEG.
    if len(frame) < 4 or not frame.startswith(b"\xff\xd8"):
        return

    with _lock:
        _latest_frame = frame
        _latest_frame_at = time.time()
        _latest_sender = sender
        _received_frames += 1


def start_udp_server(port: int, host: str = "0.0.0.0", buffer_size: int = 65_535) -> None:
    """Jalankan UDP receiver dan susun kembali chunk menjadi frame JPEG."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    print(f"[UDP] Streaming server mendengarkan di {host}:{port}")

    # Kunci: (ip, port, frame_id), nilai: metadata dan potongan frame.
    assemblies: Dict[Tuple[str, int, int], dict] = {}

    while True:
        try:
            data, addr = sock.recvfrom(buffer_size)
            now = time.time()

            # Bersihkan frame yang tidak pernah lengkap karena packet loss.
            stale_keys = [
                key
                for key, item in assemblies.items()
                if now - item["updated_at"] > ASSEMBLY_TIMEOUT_SECONDS
            ]
            for key in stale_keys:
                assemblies.pop(key, None)

            # Kompatibilitas dengan video_sender versi lama: satu datagram = satu JPEG.
            if len(data) < HEADER.size or data[:4] != MAGIC:
                _publish_frame(data, f"{addr[0]}:{addr[1]}")
                continue

            magic, frame_id, chunk_index, total_chunks = HEADER.unpack_from(data)
            if magic != MAGIC or total_chunks == 0 or chunk_index >= total_chunks:
                continue

            key = (addr[0], addr[1], frame_id)
            assembly = assemblies.setdefault(
                key,
                {
                    "total": total_chunks,
                    "chunks": {},
                    "updated_at": now,
                },
            )

            # Abaikan paket yang tidak konsisten untuk frame id yang sama.
            if assembly["total"] != total_chunks:
                assemblies.pop(key, None)
                continue

            assembly["chunks"][chunk_index] = data[HEADER.size :]
            assembly["updated_at"] = now

            if len(assembly["chunks"]) == total_chunks:
                try:
                    frame = b"".join(
                        assembly["chunks"][index] for index in range(total_chunks)
                    )
                except KeyError:
                    continue
                finally:
                    assemblies.pop(key, None)

                _publish_frame(frame, f"{addr[0]}:{addr[1]}")

        except OSError as error:
            print(f"[UDP] Socket error: {error}")
            time.sleep(0.2)
        except Exception as error:
            print(f"[UDP] Error menerima paket: {error}")

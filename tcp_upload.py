"""Server dan client upload file menggunakan socket TCP."""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
CHUNK_SIZE = 64 * 1024
MAX_FILENAME_BYTES = 255
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB batas protokol aplikasi


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Koneksi terputus saat menerima data")
        data.extend(chunk)
    return bytes(data)


def _safe_destination(filename: str) -> Path:
    clean_name = os.path.basename(filename).strip()
    if not clean_name or clean_name in {".", ".."}:
        raise ValueError("Nama file tidak valid")
    return UPLOAD_DIR / clean_name


def _handle_client(conn: socket.socket, addr) -> None:
    partial_path: Path | None = None
    try:
        conn.settimeout(60)
        name_length = int.from_bytes(_recv_exact(conn, 4), "big")
        if name_length <= 0 or name_length > MAX_FILENAME_BYTES:
            raise ValueError("Panjang nama file tidak valid")

        filename = _recv_exact(conn, name_length).decode("utf-8")
        destination = _safe_destination(filename)

        file_size = int.from_bytes(_recv_exact(conn, 8), "big")
        if file_size <= 0 or file_size > MAX_FILE_BYTES:
            raise ValueError("Ukuran file tidak valid atau melebihi 2 GB")

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        partial_path = destination.with_name(destination.name + ".part")

        received = 0
        with partial_path.open("wb") as output:
            while received < file_size:
                chunk = conn.recv(min(CHUNK_SIZE, file_size - received))
                if not chunk:
                    raise ConnectionError("Upload terputus sebelum file selesai")
                output.write(chunk)
                received += len(chunk)

        partial_path.replace(destination)
        partial_path = None
        conn.sendall(b"OK")
        print(f"[TCP] {destination.name} ({received} byte) diterima dari {addr}")

    except Exception as error:
        print(f"[TCP] Gagal menerima file dari {addr}: {error}")
        if partial_path and partial_path.exists():
            partial_path.unlink(missing_ok=True)
        try:
            conn.sendall(b"FAIL")
        except OSError:
            pass
    finally:
        conn.close()


def start_tcp_server(port: int, host: str = "127.0.0.1") -> None:
    """Jalankan TCP server; setiap client ditangani oleh thread tersendiri."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(10)
    print(f"[TCP] Upload server berjalan di {host}:{port}")

    while True:
        conn, addr = server.accept()
        threading.Thread(
            target=_handle_client,
            args=(conn, addr),
            daemon=True,
            name=f"tcp-client-{addr[0]}-{addr[1]}",
        ).start()


def _send_header(client: socket.socket, filename: str, file_size: int) -> None:
    filename_bytes = filename.encode("utf-8")
    if not filename_bytes or len(filename_bytes) > MAX_FILENAME_BYTES:
        raise ValueError("Nama file terlalu panjang untuk protokol TCP")
    if file_size <= 0 or file_size > MAX_FILE_BYTES:
        raise ValueError("Ukuran file tidak valid atau melebihi 2 GB")

    client.sendall(len(filename_bytes).to_bytes(4, "big"))
    client.sendall(filename_bytes)
    client.sendall(file_size.to_bytes(8, "big"))


def send_file_via_tcp_path(
    file_path: str | Path,
    destination_name: str,
    port: int,
    host: str = "127.0.0.1",
) -> bool:
    """Kirim file dari disk ke TCP server tanpa memuat semuanya ke RAM."""
    source = Path(file_path)
    file_size = source.stat().st_size

    with socket.create_connection((host, port), timeout=20) as client:
        client.settimeout(60)
        _send_header(client, destination_name, file_size)
        with source.open("rb") as input_file:
            while chunk := input_file.read(CHUNK_SIZE):
                client.sendall(chunk)
        response = client.recv(16)
        return response == b"OK"
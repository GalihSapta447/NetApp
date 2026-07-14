"""Client UDP untuk mengirim webcam atau file video dari perangkat lain.

Frame JPEG dikirim ke port video (default 9002). Untuk sumber berupa file,
track audio juga dapat dikirim sebagai MP3 melalui port audio (default 9003).
"""

from __future__ import annotations

import argparse
import os
import socket
import time
from pathlib import Path

import cv2
from dotenv import load_dotenv

import udp_stream
from udp_audio import UDPAudioSender

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Kirim webcam/file video ke UDP receiver NetApp PJAR."
    )
    parser.add_argument(
        "source",
        nargs="?",
        default="0",
        help="0 untuk webcam atau path file video",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("UDP_CLIENT_TARGET_HOST", "127.0.0.1"),
        help="IP perangkat server UDP",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("UDP_STREAM_PORT", "9002")),
        help="Port UDP untuk frame video",
    )
    parser.add_argument(
        "--audio-port",
        type=int,
        default=int(os.getenv("UDP_AUDIO_PORT", "9003")),
        help="Port UDP untuk audio file video",
    )
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument("--quality", type=int, default=65)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument(
        "--audio-bitrate",
        default=os.getenv("UDP_AUDIO_BITRATE", "128k"),
        help="Bitrate MP3, misalnya 96k atau 128k",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Jangan kirim track audio saat sumber berupa file video",
    )
    parser.add_argument(
        "--no-loop",
        action="store_true",
        help="Jangan ulangi file video setelah selesai",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = int(args.source) if args.source.isdigit() else args.source
    capture = cv2.VideoCapture(source)

    if not capture.isOpened():
        raise SystemExit(f"Tidak dapat membuka sumber video: {args.source}")

    source_fps = capture.get(cv2.CAP_PROP_FPS)
    fps = source_fps if source_fps and 1 <= source_fps <= 120 else args.fps
    delay = 1.0 / max(1.0, fps)
    quality = max(20, min(95, args.quality))
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    frame_id = 0
    audio_sender: UDPAudioSender | None = None

    if isinstance(source, str) and not args.no_audio:
        audio_sender = UDPAudioSender(
            args.host,
            args.audio_port,
            bitrate=args.audio_bitrate,
        )
        audio_sender.start(Path(source))
        print(
            f"[UDP-AUDIO-CLIENT] Mengirim track audio ke "
            f"{args.host}:{args.audio_port}."
        )
    elif isinstance(source, int):
        print(
            "[UDP-AUDIO-CLIENT] Webcam mengirim gambar saja. "
            "Input mikrofon belum diaktifkan."
        )

    print(
        f"[UDP-CLIENT] Mengirim {args.source!r} ke {args.host}:{args.port}. "
        "Tekan Ctrl+C untuk berhenti."
    )

    try:
        while True:
            cycle_started = time.perf_counter()
            ok, frame = capture.read()
            if not ok:
                if isinstance(source, str) and not args.no_loop:
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                break

            height, width = frame.shape[:2]
            if width > args.width:
                new_height = max(1, int(height * args.width / width))
                frame = cv2.resize(frame, (args.width, new_height))

            encoded_ok, encoded = cv2.imencode(".jpg", frame, encode_params)
            if encoded_ok:
                udp_stream.send_frame(
                    sock,
                    (args.host, args.port),
                    encoded.tobytes(),
                    frame_id,
                )
                frame_id = (frame_id + 1) & 0xFFFFFFFF

            elapsed = time.perf_counter() - cycle_started
            time.sleep(max(0.0, delay - elapsed))
    except KeyboardInterrupt:
        print("\n[UDP-CLIENT] Streaming dihentikan.")
    finally:
        if audio_sender:
            audio_sender.stop(wait=True)
        capture.release()
        sock.close()


if __name__ == "__main__":
    main()

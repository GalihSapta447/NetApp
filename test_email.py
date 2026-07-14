"""Uji konfigurasi SMTP Gmail tanpa melakukan registrasi/login."""

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from flask_mail import Mail, Message

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


username = os.getenv("MAIL_USERNAME", "").strip()
password = os.getenv("MAIL_PASSWORD", "").replace(" ", "").strip()
sender = os.getenv("MAIL_DEFAULT_SENDER", username).strip() or username

if not username or not password or "emailpengirim" in username or "app_password" in password:
    raise SystemExit(
        "Isi MAIL_USERNAME, MAIL_PASSWORD, dan MAIL_DEFAULT_SENDER pada file .env terlebih dahulu."
    )

app = Flask(__name__)
app.config.update(
    MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.gmail.com").strip(),
    MAIL_PORT=int(os.getenv("MAIL_PORT", "587")),
    MAIL_USE_TLS=env_bool("MAIL_USE_TLS", True),
    MAIL_USE_SSL=env_bool("MAIL_USE_SSL", False),
    MAIL_USERNAME=username,
    MAIL_PASSWORD=password,
    MAIL_DEFAULT_SENDER=sender,
    MAIL_TIMEOUT=int(os.getenv("MAIL_TIMEOUT", "20")),
)
mail = Mail(app)

recipient = input("Kirim email pengujian ke alamat: ").strip()
if "@" not in recipient:
    raise SystemExit("Alamat penerima tidak valid.")

with app.app_context():
    try:
        mail.send(
            Message(
                subject="Tes SMTP NetApp PJAR",
                sender=sender,
                recipients=[recipient],
                body="Konfigurasi SMTP Gmail berhasil. Sistem OTP siap digunakan.",
            )
        )
    except Exception as exc:
        raise SystemExit(f"Gagal mengirim email: {exc}") from exc

print(f"Email pengujian berhasil dikirim ke {recipient}.")

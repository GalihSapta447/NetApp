from __future__ import annotations

import os
import re
import secrets
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_mail import Mail, Message
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import db
import tcp_upload
import udp_stream
import udp_audio
from udp_video import UDPVideoStreamer

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

UPLOAD_DIR = BASE_DIR / "uploads"
UDP_VIDEO_DIR = BASE_DIR / "udp_videos"
TEMP_UPLOAD_DIR = BASE_DIR / "temp_uploads"
for directory in (UPLOAD_DIR, UDP_VIDEO_DIR, TEMP_UPLOAD_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-ganti-ini")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "512")) * 1024 * 1024


# ---------- Konfigurasi umum ----------
def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# ---------- Konfigurasi Email ----------
mail_username = os.getenv("MAIL_USERNAME", "").strip()
mail_password = os.getenv("MAIL_PASSWORD", "").replace(" ", "").strip()
mail_sender = os.getenv("MAIL_DEFAULT_SENDER", mail_username).strip()

app.config.update(
    MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.gmail.com").strip(),
    MAIL_PORT=int(os.getenv("MAIL_PORT", "587")),
    MAIL_USE_TLS=env_bool("MAIL_USE_TLS", True),
    MAIL_USE_SSL=env_bool("MAIL_USE_SSL", False),
    MAIL_USERNAME=mail_username,
    MAIL_PASSWORD=mail_password,
    MAIL_DEFAULT_SENDER=mail_sender or mail_username,
    MAIL_TIMEOUT=int(os.getenv("MAIL_TIMEOUT", "20")),
    MAIL_SUPPRESS_SEND=False,
)
mail = Mail(app)
MAIL_MODE = os.getenv("MAIL_MODE", "smtp").strip().lower()
SHOW_OTP_IN_TERMINAL = env_bool("SHOW_OTP_IN_TERMINAL", False)

# ---------- Konfigurasi OTP ----------
OTP_TTL_SECONDS = int(os.getenv("OTP_TTL_SECONDS", "300"))
OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))
OTP_RESEND_COOLDOWN = int(os.getenv("OTP_RESEND_COOLDOWN", "30"))

# ---------- Konfigurasi Socket ----------
TCP_HOST = os.getenv("TCP_UPLOAD_HOST", "127.0.0.1").strip()
TCP_BIND_HOST = os.getenv("TCP_UPLOAD_BIND_HOST", "127.0.0.1").strip()
TCP_PORT = int(os.getenv("TCP_UPLOAD_PORT", "9001"))

UDP_BIND_HOST = os.getenv(
    "UDP_STREAM_BIND_HOST", os.getenv("UDP_STREAM_HOST", "0.0.0.0")
).strip()
UDP_TARGET_HOST = os.getenv("UDP_TARGET_HOST", "127.0.0.1").strip()
UDP_PORT = int(os.getenv("UDP_STREAM_PORT", "9002"))
UDP_AUDIO_BIND_HOST = os.getenv("UDP_AUDIO_BIND_HOST", "0.0.0.0").strip()
UDP_AUDIO_PORT = int(os.getenv("UDP_AUDIO_PORT", "9003"))
WEB_PORT = int(os.getenv("PORT", "5000"))

udp_video_streamer = UDPVideoStreamer(
    UDP_TARGET_HOST,
    UDP_PORT,
    jpeg_quality=int(os.getenv("UDP_JPEG_QUALITY", "65")),
    target_width=int(os.getenv("UDP_FRAME_WIDTH", "640")),
    fallback_fps=float(os.getenv("UDP_FPS", "20")),
    audio_target_port=UDP_AUDIO_PORT,
    audio_bitrate=os.getenv("UDP_AUDIO_BITRATE", "128k").strip(),
)

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".mpeg", ".mpg"}
INLINE_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".csv",
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp4",
    ".webm",
    ".mp3",
    ".wav",
}


# ---------- Helper autentikasi ----------
def is_authenticated() -> bool:
    return bool(session.get("user_id"))


def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            flash("Silakan login terlebih dahulu.", "warning")
            return redirect(url_for("login"))
        return function(*args, **kwargs)

    return wrapper


def api_login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            return jsonify({"ok": False, "error": "Sesi login berakhir."}), 401
        return function(*args, **kwargs)

    return wrapper


def set_pending_registration(user):
    session.clear()
    session["pending_registration_user_id"] = user["id"]
    session["pending_registration_email"] = user["email"]


def clear_pending_registration():
    session.pop("pending_registration_user_id", None)
    session.pop("pending_registration_email", None)


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        hidden_local = local[:1] + "*"
    else:
        hidden_local = local[:2] + "*" * max(2, len(local) - 2)
    return f"{hidden_local}@{domain}"


# ---------- Helper OTP dan Email ----------
def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def send_registration_otp_email(email: str, username: str, otp: str) -> bool:
    subject = "Kode OTP Registrasi - NetApp PJAR"
    action = "mengaktifkan akun"

    ttl_minutes = max(1, OTP_TTL_SECONDS // 60)
    body = (
        f"Halo {username},\n\n"
        f"Gunakan kode OTP berikut untuk {action}:\n\n"
        f"{otp}\n\n"
        f"Kode berlaku selama {ttl_minutes} menit dan hanya dapat digunakan "
        "satu kali. Jangan berikan kode ini kepada siapa pun.\n\n"
        "Abaikan email ini apabila Anda tidak melakukan permintaan tersebut."
    )
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;line-height:1.6">
      <h2 style="margin-bottom:8px">NetApp PJAR</h2>
      <p>Halo <strong>{username}</strong>,</p>
      <p>Gunakan kode berikut untuk {action}:</p>
      <div style="font-size:32px;font-weight:bold;letter-spacing:8px;
                  padding:16px 20px;background:#f3f4f6;border-radius:10px;
                  text-align:center">{otp}</div>
      <p>Kode berlaku selama <strong>{ttl_minutes} menit</strong> dan hanya dapat
         digunakan satu kali.</p>
      <p>Jangan berikan kode ini kepada siapa pun.</p>
      <p style="color:#666;font-size:13px">Abaikan email ini apabila Anda tidak
         melakukan permintaan tersebut.</p>
    </div>
    """

    if MAIL_MODE == "console":
        print(f"[OTP-REGISTRATION] {email}: {otp}")
        return True

    if MAIL_MODE != "smtp":
        print(f"[EMAIL] MAIL_MODE tidak dikenal: {MAIL_MODE!r}. Gunakan 'smtp'.")
        return False

    if not app.config.get("MAIL_USERNAME") or not app.config.get("MAIL_PASSWORD"):
        print("[EMAIL] MAIL_USERNAME atau MAIL_PASSWORD belum diisi pada file .env.")
        return False

    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config["MAIL_USERNAME"]
    message = Message(
        subject=subject,
        sender=sender,
        recipients=[email],
        body=body,
        html=html,
    )

    try:
        mail.send(message)
        print(f"[EMAIL] OTP registrasi berhasil dikirim ke {email}")
        if SHOW_OTP_IN_TERMINAL:
            print(f"[DEBUG-OTP-REGISTRATION] {email}: {otp}")
        return True
    except Exception as error:
        error_text = str(error)
        print(f"[EMAIL] Gagal mengirim OTP registrasi: {error_text}")
        if "Application-specific password required" in error_text or "534" in error_text:
            print(
                "[EMAIL] Aktifkan Verifikasi 2 Langkah dan gunakan App Password "
                "16 karakter, bukan password Gmail biasa."
            )
        elif "Username and Password not accepted" in error_text or "535" in error_text:
            print("[EMAIL] Periksa MAIL_USERNAME dan MAIL_PASSWORD pada file .env.")
        return False


def create_registration_otp(user) -> bool:
    otp = generate_otp()
    db.save_registration_otp(
        user_id=user["id"],
        otp_hash=generate_password_hash(otp),
        expires_at=int(time.time()) + OTP_TTL_SECONDS,
    )
    return send_registration_otp_email(user["email"], user["username"], otp)


def resend_wait_seconds(otp_record) -> int:
    if not otp_record:
        return 0
    elapsed = int(time.time()) - int(otp_record["issued_at"] or 0)
    return max(0, OTP_RESEND_COOLDOWN - elapsed)


def validate_otp_input(otp_input: str, otp_record, increment_attempt, mark_used):
    if not otp_input.isdigit() or len(otp_input) != 6:
        return False, "OTP harus terdiri dari tepat 6 angka."
    if not otp_record:
        return False, "OTP tidak tersedia. Silakan kirim ulang OTP."
    if otp_record["is_used"]:
        return False, "OTP sudah digunakan. Silakan kirim ulang OTP."
    if int(time.time()) > int(otp_record["expires_at"]):
        mark_used(otp_record["id"])
        return False, "OTP sudah kedaluwarsa. Silakan kirim ulang OTP."
    if int(otp_record["attempts"]) >= OTP_MAX_ATTEMPTS:
        mark_used(otp_record["id"])
        return False, "Batas percobaan OTP telah habis. Silakan kirim ulang OTP."
    if not check_password_hash(otp_record["otp_hash"], otp_input):
        increment_attempt(otp_record["id"])
        remaining = OTP_MAX_ATTEMPTS - int(otp_record["attempts"]) - 1
        return False, f"Kode OTP salah. Sisa percobaan: {max(0, remaining)}."
    mark_used(otp_record["id"])
    return True, ""


# ---------- Helper file ----------
def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def unique_filename(directory: Path, original_name: str) -> str:
    clean = secure_filename(original_name)
    if not clean:
        raise ValueError("Nama file tidak valid")

    candidate = directory / clean
    if not candidate.exists():
        return clean

    stem = Path(clean).stem
    suffix = Path(clean).suffix
    for index in range(1, 10_000):
        candidate_name = f"{stem}_{index}{suffix}"
        if not (directory / candidate_name).exists():
            return candidate_name
    raise ValueError("Tidak dapat membuat nama file unik")


def resolve_existing_file(directory: Path, filename: str) -> Path:
    clean = secure_filename(Path(filename).name)
    if not clean or clean != Path(filename).name:
        abort(404)
    path = (directory / clean).resolve()
    if path.parent != directory.resolve() or not path.is_file():
        abort(404)
    return path


def list_stored_files(directory: Path) -> list[dict]:
    records = []
    for path in directory.iterdir():
        if not path.is_file() or path.name.startswith(".") or path.suffix == ".part":
            continue
        stat = path.stat()
        records.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "size_text": human_size(stat.st_size),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M"),
                "can_preview": path.suffix.lower() in INLINE_EXTENSIONS,
            }
        )
    return sorted(records, key=lambda item: item["name"].lower())


@app.errorhandler(413)
def file_too_large(_error):
    max_mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    flash(f"Ukuran file melebihi batas {max_mb} MB.", "danger")
    return redirect(request.referrer or url_for("dashboard"))


# ---------- Halaman utama ----------
@app.route("/")
def index():
    if is_authenticated():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


# ---------- Register + OTP Registrasi ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if is_authenticated():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not username or not email or not password or not confirm_password:
            flash("Semua kolom wajib diisi.", "danger")
            return redirect(url_for("register"))
        if len(username) < 3 or len(username) > 30:
            flash("Username harus terdiri dari 3 sampai 30 karakter.", "danger")
            return redirect(url_for("register"))
        if not EMAIL_PATTERN.match(email):
            flash("Format email tidak valid.", "danger")
            return redirect(url_for("register"))
        if len(password) < 8:
            flash("Password minimal 8 karakter.", "danger")
            return redirect(url_for("register"))
        if password != confirm_password:
            flash("Konfirmasi password tidak cocok.", "danger")
            return redirect(url_for("register"))

        existing_email = db.get_user_by_email(email)
        existing_username = db.get_user_by_username(username)

        if existing_email:
            if existing_email["is_verified"]:
                flash("Email sudah terdaftar. Silakan login.", "warning")
                return redirect(url_for("login"))
            set_pending_registration(existing_email)
            wait = resend_wait_seconds(db.get_active_registration_otp(existing_email["id"]))
            if wait > 0:
                flash(
                    f"Akun belum aktif. Tunggu {wait} detik untuk mengirim ulang OTP.",
                    "warning",
                )
            elif create_registration_otp(existing_email):
                flash("OTP registrasi baru dikirim ke email.", "success")
            else:
                flash("OTP gagal dikirim. Periksa konfigurasi email.", "danger")
            return redirect(url_for("verify_registration_otp"))

        if existing_username:
            flash("Username sudah digunakan.", "danger")
            return redirect(url_for("register"))

        user_id = db.create_user(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
        )
        user = db.get_user_by_id(user_id)
        set_pending_registration(user)

        if create_registration_otp(user):
            flash("Registrasi berhasil. OTP dikirim ke email Anda.", "success")
        else:
            flash(
                "Akun dibuat, tetapi OTP gagal dikirim. Periksa .env lalu kirim ulang OTP.",
                "danger",
            )
        return redirect(url_for("verify_registration_otp"))

    return render_template("register.html")


@app.route("/verify-registration-otp", methods=["GET", "POST"])
def verify_registration_otp():
    user_id = session.get("pending_registration_user_id")
    email = session.get("pending_registration_email")
    if not user_id or not email:
        flash("Sesi verifikasi registrasi tidak ditemukan.", "warning")
        return redirect(url_for("register"))

    user = db.get_user_by_id(user_id)
    if not user or user["email"].lower() != email.lower():
        clear_pending_registration()
        flash("Sesi verifikasi tidak valid.", "danger")
        return redirect(url_for("register"))

    if user["is_verified"]:
        clear_pending_registration()
        flash("Akun sudah aktif. Silakan login.", "info")
        return redirect(url_for("login"))

    if request.method == "POST":
        otp_input = request.form.get("otp", "").strip()
        valid, error = validate_otp_input(
            otp_input,
            db.get_active_registration_otp(user_id),
            db.increment_registration_otp_attempts,
            db.mark_registration_otp_used,
        )
        if not valid:
            flash(error, "danger")
            return redirect(url_for("verify_registration_otp"))

        db.mark_verified(user_id)
        clear_pending_registration()
        flash("Akun berhasil diverifikasi. Silakan login.", "success")
        return redirect(url_for("login"))

    return render_template(
        "verify_registration_otp.html",
        email=mask_email(email),
        ttl_minutes=max(1, OTP_TTL_SECONDS // 60),
    )


@app.route("/resend-registration-otp", methods=["POST"])
def resend_registration_otp():
    user_id = session.get("pending_registration_user_id")
    user = db.get_user_by_id(user_id) if user_id else None
    if not user or user["is_verified"]:
        clear_pending_registration()
        flash("Sesi registrasi tidak tersedia.", "warning")
        return redirect(url_for("login"))

    wait = resend_wait_seconds(db.get_active_registration_otp(user_id))
    if wait > 0:
        flash(f"Tunggu {wait} detik sebelum mengirim ulang OTP.", "warning")
    elif create_registration_otp(user):
        flash("OTP registrasi baru berhasil dikirim.", "success")
    else:
        flash("OTP gagal dikirim. Periksa konfigurasi email.", "danger")
    return redirect(url_for("verify_registration_otp"))


@app.route("/cancel-registration", methods=["POST"])
def cancel_registration():
    clear_pending_registration()
    flash("Proses verifikasi registrasi dibatalkan.", "info")
    return redirect(url_for("login"))


# ---------- Login menggunakan email dan password ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if is_authenticated():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Email dan password wajib diisi.", "danger")
            return redirect(url_for("login"))

        user = db.get_user_by_email(email)
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Email atau password salah.", "danger")
            return redirect(url_for("login"))

        if not user["is_verified"]:
            set_pending_registration(user)
            if resend_wait_seconds(db.get_active_registration_otp(user["id"])) == 0:
                create_registration_otp(user)
            flash("Akun belum aktif. Masukkan OTP registrasi.", "warning")
            return redirect(url_for("verify_registration_otp"))

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["email"] = user["email"]
        flash(f"Login berhasil. Selamat datang, {user['username']}!", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Anda sudah logout.", "info")
    return redirect(url_for("login"))


# ---------- Dashboard dan Live Chat ----------
@app.route("/dashboard")
@login_required
def dashboard():
    return render_template(
        "dashboard.html",
        username=session.get("username"),
        tcp_file_count=len(list_stored_files(UPLOAD_DIR)),
        udp_file_count=len(list_stored_files(UDP_VIDEO_DIR)),
    )


@app.route("/api/chat/messages", methods=["GET"])
@api_login_required
def chat_messages():
    try:
        after_id = max(0, int(request.args.get("after_id", "0")))
    except ValueError:
        after_id = 0

    rows = (
        db.get_recent_chat_messages(50)
        if after_id == 0
        else db.get_chat_messages(after_id=after_id, limit=100)
    )
    current_user_id = int(session["user_id"])
    messages = [
        {
            "id": row["id"],
            "username": row["username"],
            "message": row["message"],
            "created_at": row["created_at"],
            "mine": row["user_id"] == current_user_id,
        }
        for row in rows
    ]
    return jsonify({"ok": True, "messages": messages})


@app.route("/api/chat/messages", methods=["POST"])
@api_login_required
def send_chat_message():
    payload = request.get_json(silent=True) or request.form
    message = str(payload.get("message", "")).strip()

    if not message:
        return jsonify({"ok": False, "error": "Pesan tidak boleh kosong."}), 400
    if len(message) > 500:
        return jsonify({"ok": False, "error": "Pesan maksimal 500 karakter."}), 400

    now = time.time()
    previous = float(session.get("last_chat_at", 0))
    if now - previous < 0.5:
        return jsonify({"ok": False, "error": "Pesan dikirim terlalu cepat."}), 429

    message_id = db.create_chat_message(int(session["user_id"]), message)
    session["last_chat_at"] = now
    return jsonify({"ok": True, "message_id": message_id}), 201


# ---------- Upload dan pengelolaan file TCP ----------
@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        uploaded_file = request.files.get("file")
        if not uploaded_file or not uploaded_file.filename:
            flash("Pilih file terlebih dahulu.", "danger")
            return redirect(url_for("upload"))

        try:
            filename = unique_filename(UPLOAD_DIR, uploaded_file.filename)
        except ValueError as error:
            flash(str(error), "danger")
            return redirect(url_for("upload"))

        temporary_path = TEMP_UPLOAD_DIR / f"tcp-{secrets.token_hex(12)}.tmp"
        try:
            uploaded_file.save(temporary_path)
            if temporary_path.stat().st_size == 0:
                flash("File kosong tidak dapat dikirim.", "danger")
                return redirect(url_for("upload"))

            success = tcp_upload.send_file_via_tcp_path(
                temporary_path,
                filename,
                TCP_PORT,
                host=TCP_HOST,
            )
        except Exception as error:
            flash(f"Gagal mengirim file ke TCP server: {error}", "danger")
            return redirect(url_for("upload"))
        finally:
            temporary_path.unlink(missing_ok=True)

        if success:
            flash(f"File '{filename}' berhasil dikirim melalui TCP.", "success")
        else:
            flash("TCP server tidak memberikan konfirmasi berhasil.", "danger")
        return redirect(url_for("upload"))

    return render_template("upload.html", files=list_stored_files(UPLOAD_DIR), tcp_port=TCP_PORT)


@app.route("/tcp/files/<path:filename>/view")
@login_required
def tcp_view_file(filename: str):
    path = resolve_existing_file(UPLOAD_DIR, filename)
    return send_from_directory(path.parent, path.name, as_attachment=False, conditional=True)


@app.route("/tcp/files/<path:filename>/download")
@login_required
def tcp_download_file(filename: str):
    path = resolve_existing_file(UPLOAD_DIR, filename)
    return send_from_directory(path.parent, path.name, as_attachment=True)


@app.route("/tcp/files/<path:filename>/delete", methods=["POST"])
@login_required
def tcp_delete_file(filename: str):
    path = resolve_existing_file(UPLOAD_DIR, filename)
    try:
        path.unlink()
        flash(f"File TCP '{path.name}' berhasil dihapus.", "success")
    except OSError as error:
        flash(f"File TCP gagal dihapus: {error}", "danger")
    return redirect(url_for("upload"))


# ---------- Upload dan streaming file video melalui UDP ----------
@app.route("/stream")
@login_required
def stream_page():
    return render_template(
        "stream.html",
        videos=list_stored_files(UDP_VIDEO_DIR),
        udp_port=UDP_PORT,
        sender_status=udp_video_streamer.status(),
        receiver_status=udp_stream.get_stream_status(),
        audio_receiver_status=udp_audio.get_audio_status(),
        audio_port=UDP_AUDIO_PORT,
    )


@app.route("/udp/upload", methods=["POST"])
@login_required
def udp_upload_video():
    uploaded_file = request.files.get("video")
    if not uploaded_file or not uploaded_file.filename:
        flash("Pilih file video terlebih dahulu.", "danger")
        return redirect(url_for("stream_page"))

    extension = Path(uploaded_file.filename).suffix.lower()
    if extension not in VIDEO_EXTENSIONS:
        flash(
            "Format video tidak didukung. Gunakan MP4, AVI, MOV, MKV, WEBM, MPEG, atau MPG.",
            "danger",
        )
        return redirect(url_for("stream_page"))

    try:
        filename = unique_filename(UDP_VIDEO_DIR, uploaded_file.filename)
        destination = UDP_VIDEO_DIR / filename
        uploaded_file.save(destination)
        if destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            flash("File video kosong tidak dapat disimpan.", "danger")
            return redirect(url_for("stream_page"))

        if request.form.get("autostart") == "1":
            udp_video_streamer.start(destination)
            flash(f"Video '{filename}' diunggah dan mulai dikirim melalui UDP.", "success")
        else:
            flash(f"Video '{filename}' berhasil diunggah.", "success")
    except Exception as error:
        flash(f"Upload video UDP gagal: {error}", "danger")

    return redirect(url_for("stream_page"))


@app.route("/udp/files/<path:filename>/start", methods=["POST"])
@login_required
def udp_start_video(filename: str):
    path = resolve_existing_file(UDP_VIDEO_DIR, filename)
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        flash("File tersebut bukan format video yang didukung.", "danger")
        return redirect(url_for("stream_page"))

    try:
        udp_video_streamer.start(path)
        flash(f"Streaming UDP '{path.name}' dimulai.", "success")
    except Exception as error:
        flash(f"Streaming UDP gagal dimulai: {error}", "danger")
    return redirect(url_for("stream_page"))


@app.route("/udp/stop", methods=["POST"])
@login_required
def udp_stop_video():
    udp_video_streamer.stop(wait=False)
    flash("Pengiriman file video melalui UDP dihentikan.", "info")
    return redirect(url_for("stream_page"))


@app.route("/udp/files/<path:filename>/view")
@login_required
def udp_view_file(filename: str):
    path = resolve_existing_file(UDP_VIDEO_DIR, filename)
    return send_from_directory(path.parent, path.name, as_attachment=False, conditional=True)


@app.route("/udp/files/<path:filename>/delete", methods=["POST"])
@login_required
def udp_delete_file(filename: str):
    path = resolve_existing_file(UDP_VIDEO_DIR, filename)
    status = udp_video_streamer.status()
    if status.get("current_file") == path.name:
        udp_video_streamer.stop(wait=True)

    try:
        path.unlink()
        flash(f"File video UDP '{path.name}' berhasil dihapus.", "success")
    except OSError as error:
        flash(f"File video UDP gagal dihapus: {error}", "danger")
    return redirect(url_for("stream_page"))


@app.route("/api/udp/status")
@api_login_required
def udp_status():
    return jsonify(
        {
            "ok": True,
            "sender": udp_video_streamer.status(),
            "receiver": udp_stream.get_stream_status(),
            "audio_receiver": udp_audio.get_audio_status(),
        }
    )


@app.route("/api/udp/reload", methods=["POST"])
@api_login_required
def udp_reload_video():
    """Mulai ulang video aktif dari frame pertama."""

    try:
        udp_video_streamer.restart()
    except RuntimeError as error:
        return jsonify({"ok": False, "error": str(error)}), 409
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "File video aktif sudah tidak ditemukan."}), 404
    except Exception as error:
        return jsonify({"ok": False, "error": f"Video gagal dimuat ulang: {error}"}), 500

    return jsonify(
        {
            "ok": True,
            "message": "Video dimuat ulang dari awal.",
            "sender": udp_video_streamer.status(),
            "receiver": udp_stream.get_stream_status(),
            "audio_receiver": udp_audio.get_audio_status(),
        }
    )


def mjpeg_generator():
    last_frame = None
    while True:
        frame = udp_stream.get_latest_frame()
        if frame is not None and frame is not last_frame:
            last_frame = frame
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        time.sleep(0.03)


@app.route("/video_feed")
@login_required
def video_feed():
    return Response(
        mjpeg_generator(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.route("/audio_feed")
@login_required
def audio_feed():
    return Response(
        udp_audio.audio_stream_generator(),
        mimetype="audio/mpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------- Startup server TCP dan UDP ----------
def start_background_servers():
    threading.Thread(
        target=tcp_upload.start_tcp_server,
        args=(TCP_PORT, TCP_BIND_HOST),
        daemon=True,
        name="tcp-upload-server",
    ).start()
    threading.Thread(
        target=udp_stream.start_udp_server,
        args=(UDP_PORT, UDP_BIND_HOST),
        daemon=True,
        name="udp-stream-server",
    ).start()
    threading.Thread(
        target=udp_audio.start_udp_audio_server,
        args=(UDP_AUDIO_PORT, UDP_AUDIO_BIND_HOST),
        daemon=True,
        name="udp-audio-server",
    ).start()


def print_mail_status():
    if MAIL_MODE == "console":
        print("[EMAIL] Mode console aktif: OTP hanya tampil di terminal.")
        return

    username = app.config.get("MAIL_USERNAME", "")
    if not username or not app.config.get("MAIL_PASSWORD"):
        print("[EMAIL] SMTP belum siap: isi MAIL_USERNAME dan MAIL_PASSWORD di .env.")
        return

    local, sep, domain = username.partition("@")
    masked = (local[:2] + "***" + sep + domain) if sep else "***"
    print(
        f"[EMAIL] Mode SMTP aktif | server={app.config['MAIL_SERVER']}:"
        f"{app.config['MAIL_PORT']} | pengirim={masked}"
    )


if __name__ == "__main__":
    db.init_db()
    print_mail_status()
    start_background_servers()
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False, use_reloader=False, threaded=True)
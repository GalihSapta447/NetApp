import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().with_name("app.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn, table: str, column: str, definition: str):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    """Membuat/migrasikan tabel autentikasi tanpa menghapus akun lama."""
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS registration_otps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                otp_hash TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                is_used INTEGER NOT NULL DEFAULT 0,
                issued_at INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_id ON chat_messages(id)"
        )

        # Kompatibel jika tabel OTP registrasi dibuat dengan versi lama.
        _ensure_column(conn, "registration_otps", "attempts", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "registration_otps", "is_used", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "registration_otps", "issued_at", "INTEGER NOT NULL DEFAULT 0")

        # OTP login sudah tidak digunakan; login cukup dengan email dan password.
        conn.execute("DROP TABLE IF EXISTS login_otps")
        conn.commit()


def create_user(username: str, email: str, password_hash: str):
    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
            (username, email, password_hash),
        )
        conn.commit()
        return cursor.lastrowid


def get_user_by_email(email: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?)", (email,)
        ).fetchone()


def get_user_by_username(username: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE lower(username) = lower(?)", (username,)
        ).fetchone()


def get_user_by_id(user_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def mark_verified(user_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (user_id,))
        conn.commit()


def save_registration_otp(user_id: int, otp_hash: str, expires_at: int):
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            "UPDATE registration_otps SET is_used = 1 WHERE user_id = ?",
            (user_id,),
        )
        conn.execute(
            """
            INSERT INTO registration_otps (user_id, otp_hash, expires_at, issued_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, otp_hash, expires_at, now),
        )
        conn.commit()


def get_active_registration_otp(user_id: int):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM registration_otps
            WHERE user_id = ? AND is_used = 0
            ORDER BY id DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()


def mark_registration_otp_used(otp_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE registration_otps SET is_used = 1 WHERE id = ?",
            (otp_id,),
        )
        conn.commit()


def increment_registration_otp_attempts(otp_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE registration_otps SET attempts = attempts + 1 WHERE id = ?",
            (otp_id,),
        )
        conn.commit()


# ---------- Live Chat ----------
def create_chat_message(user_id: int, message: str):
    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO chat_messages (user_id, message) VALUES (?, ?)",
            (user_id, message),
        )
        conn.commit()
        return cursor.lastrowid


def get_chat_messages(after_id: int = 0, limit: int = 100):
    safe_limit = max(1, min(int(limit), 200))
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT
                chat_messages.id,
                chat_messages.user_id,
                chat_messages.message,
                chat_messages.created_at,
                users.username
            FROM chat_messages
            JOIN users ON users.id = chat_messages.user_id
            WHERE chat_messages.id > ?
            ORDER BY chat_messages.id ASC
            LIMIT ?
            """,
            (max(0, int(after_id)), safe_limit),
        ).fetchall()


def get_recent_chat_messages(limit: int = 50):
    safe_limit = max(1, min(int(limit), 200))
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                chat_messages.id,
                chat_messages.user_id,
                chat_messages.message,
                chat_messages.created_at,
                users.username
            FROM chat_messages
            JOIN users ON users.id = chat_messages.user_id
            ORDER BY chat_messages.id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        return list(reversed(rows))
import hashlib
import random
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from multiclaw.auth.models import User, VerificationCode
from multiclaw.sqlite_utils import configure_sqlite_connection

CODE_EXPIRY_MINUTES = 15
MAX_SENDS_PER_DAY = 3


class AuthStore:
    def __init__(self, database_path: str) -> None:
        self._database_path = database_path
        self._db: aiosqlite.Connection | None = None
        self.jwt_secret: str = ""
        self._verification_codes_layout: str = "legacy"

    async def initialize(self) -> None:
        Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._database_path)
        self._db.row_factory = aiosqlite.Row
        await configure_sqlite_connection(self._db)
        # Auth tables
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS verification_codes (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                code TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_verification_email
            ON verification_codes(email, created_at DESC)
            """
        )
        # config table for jwt_secret persistence
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        # Load or generate jwt_secret
        cursor = await self._db.execute(
            "SELECT value FROM auth_config WHERE key = 'jwt_secret'"
        )
        row = await cursor.fetchone()
        if row:
            self.jwt_secret = row["value"]
        else:
            self.jwt_secret = secrets.token_hex(32)
            await self._db.execute(
                "INSERT INTO auth_config (key, value) VALUES ('jwt_secret', ?)",
                (self.jwt_secret,),
            )

        await self._migrate_sessions()
        await self._detect_verification_code_layout()
        await self._db.commit()

    async def _migrate_sessions(self) -> None:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_sessions'"
        )
        row = await cursor.fetchone()
        if row is None:
            return
        cursor = await self._db.execute("PRAGMA table_info(chat_sessions)")
        columns = [row[1] for row in await cursor.fetchall()]
        if "user_id" not in columns:
            await self._db.execute(
                "ALTER TABLE chat_sessions ADD COLUMN user_id TEXT NOT NULL DEFAULT ''"
            )

    async def _detect_verification_code_layout(self) -> None:
        assert self._db is not None
        cursor = await self._db.execute("PRAGMA table_info(verification_codes)")
        columns = {row[1] for row in await cursor.fetchall()}
        self._verification_codes_layout = "frozen" if "code_digest" in columns else "legacy"

    @staticmethod
    def build_code(email: str, code: str | None = None) -> VerificationCode:
        code_str = code or f"{random.randint(0, 999999):06d}"
        now = datetime.now(timezone.utc)
        return VerificationCode(
            email=email,
            code=code_str,
            expires_at=now + timedelta(minutes=CODE_EXPIRY_MINUTES),
            created_at=now,
        )

    async def save_code(self, vc: VerificationCode) -> None:
        db = await self._ensure_db()
        if self._verification_codes_layout == "frozen":
            await db.execute(
                """
                INSERT INTO verification_codes (
                    id, email, code_digest, purpose, expires_at, used_at, created_at
                )
                VALUES (?, ?, ?, 'login', ?, NULL, ?)
                """,
                (
                    vc.id,
                    vc.email,
                    _code_digest(vc.code),
                    _to_epoch_ms(vc.expires_at),
                    _to_epoch_ms(vc.created_at),
                ),
            )
        else:
            await db.execute(
                """
                INSERT INTO verification_codes (id, email, code, expires_at, used, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (vc.id, vc.email, vc.code, vc.expires_at.isoformat(), vc.created_at.isoformat()),
            )
        await db.commit()

    async def count_recent_sends(self, email: str) -> int:
        db = await self._ensure_db()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        if self._verification_codes_layout == "frozen":
            cursor = await db.execute(
                """
                SELECT COUNT(*)
                FROM verification_codes
                WHERE email = ? AND purpose = 'login' AND created_at > ?
                """,
                (email, _to_epoch_ms(cutoff)),
            )
        else:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM verification_codes WHERE email = ? AND created_at > ?",
                (email, cutoff.isoformat()),
            )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def find_latest_unused_code(self, email: str, code: str) -> VerificationCode | None:
        db = await self._ensure_db()
        if self._verification_codes_layout == "frozen":
            cursor = await db.execute(
                """
                SELECT * FROM verification_codes
                WHERE email = ?
                  AND purpose = 'login'
                  AND used_at IS NULL
                  AND expires_at > ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (email, _to_epoch_ms(datetime.now(timezone.utc))),
            )
        else:
            cursor = await db.execute(
                """
                SELECT * FROM verification_codes
                WHERE email = ? AND used = 0 AND expires_at > ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (email, datetime.now(timezone.utc).isoformat()),
            )
        row = await cursor.fetchone()
        if row is None:
            return None
        if self._verification_codes_layout == "frozen":
            if not secrets.compare_digest(str(row["code_digest"]), _code_digest(code)):
                return None
        elif row["code"] != code:
            return None
        return VerificationCode(
            id=row["id"],
            email=row["email"],
            code=code,
            expires_at=(
                _from_epoch_ms(row["expires_at"])
                if self._verification_codes_layout == "frozen"
                else row["expires_at"]
            ),
            used=(
                row["used_at"] is not None
                if self._verification_codes_layout == "frozen"
                else bool(row["used"])
            ),
            created_at=(
                _from_epoch_ms(row["created_at"])
                if self._verification_codes_layout == "frozen"
                else row["created_at"]
            ),
        )

    async def mark_code_used(self, code_id: str) -> None:
        db = await self._ensure_db()
        if self._verification_codes_layout == "frozen":
            await db.execute(
                "UPDATE verification_codes SET used_at = ? WHERE id = ?",
                (_to_epoch_ms(datetime.now(timezone.utc)), code_id),
            )
        else:
            await db.execute(
                "UPDATE verification_codes SET used = 1 WHERE id = ?",
                (code_id,),
            )
        await db.commit()

    async def get_or_create_user(self, email: str) -> User:
        db = await self._ensure_db()
        # Atomic: insert if not exists, then select unconditionally
        user = User(email=email)
        await db.execute(
            "INSERT OR IGNORE INTO users (id, email, created_at) VALUES (?, ?, ?)",
            (user.id, user.email, user.created_at.isoformat()),
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = await cursor.fetchone()
        assert row is not None
        return User(
            id=row["id"],
            email=row["email"],
            created_at=row["created_at"],
        )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            await self.initialize()
        assert self._db is not None
        return self._db


def _code_digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _to_epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _from_epoch_ms(value: int | str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)

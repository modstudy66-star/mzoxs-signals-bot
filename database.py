"""طبقة التخزين المحلي للبوت باستخدام SQLite.

لا تُحفظ أي أسرار أو رموز وصول في قاعدة البيانات. تُدار البيانات الحساسة
مثل توكن البوت حصراً من خلال ملف البيئة .env.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


class Database:
    """واجهة صغيرة ومنظمة للتعامل مع قاعدة بيانات البوت."""

    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """يفتح اتصالاً قصير العمر مع تفعيل المفاتيح الخارجية."""
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """ينشئ الجداول والفهارس عند أول تشغيل فقط."""
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    subscription_until TEXT,
                    access_revoked INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    image_file_id TEXT,
                    button_text TEXT,
                    button_url TEXT,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_sent_at TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS payments (
                    telegram_payment_charge_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    invoice_payload TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    total_amount INTEGER NOT NULL,
                    paid_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS offer_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    delivered_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    FOREIGN KEY (offer_id) REFERENCES offers(id),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_users_subscription
                    ON users(subscription_until);
                CREATE INDEX IF NOT EXISTS idx_offers_active_last_sent
                    ON offers(is_active, last_sent_at);
                """
            )
            # يدعم الترقية الآمنة من النسخة النصية السابقة دون فقدان العروض.
            self._ensure_offer_columns(conn)

    @staticmethod
    def _ensure_offer_columns(conn: sqlite3.Connection) -> None:
        """يضيف حقول الوسائط إلى قاعدة قديمة إن كانت موجودة قبل التحديث."""
        current_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(offers)").fetchall()
        }
        required_columns = {
            "image_file_id": "TEXT",
            "button_text": "TEXT",
            "button_url": "TEXT",
        }
        for column_name, column_type in required_columns.items():
            if column_name not in current_columns:
                conn.execute(f"ALTER TABLE offers ADD COLUMN {column_name} {column_type}")

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _utc_now_iso(cls) -> str:
        return cls._utc_now().isoformat()

    def upsert_user(self, user_id: int, username: str | None, full_name: str) -> None:
        """يسجل مستخدماً جديداً أو يحدث معلومات مستخدم حالي."""
        now = self._utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, first_seen_at, last_seen_at, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    last_seen_at = excluded.last_seen_at,
                    is_active = 1
                """,
                (user_id, username, full_name, now, now),
            )

    def set_user_inactive(self, user_id: int) -> None:
        """يوقف الإرسال لمستخدم حظر البوت أو لم يعد متاحاً."""
        with self._connection() as conn:
            conn.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))

    def add_offer(
        self,
        text: str,
        created_by: int,
        image_file_id: str | None = None,
        button_text: str | None = None,
        button_url: str | None = None,
    ) -> int:
        """يضيف عرضاً مع صورة اختيارية وزر رابط ويعيد رقمه الداخلي."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO offers
                    (text, image_file_id, button_text, button_url, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    text.strip(),
                    image_file_id,
                    button_text,
                    button_url,
                    created_by,
                    self._utc_now_iso(),
                ),
            )
            return int(cursor.lastrowid)

    def get_active_offers(self, limit: int = 20) -> list[sqlite3.Row]:
        """يعيد قائمة العروض المتاحة مرتبة من الأحدث إلى الأقدم."""
        with self._connection() as conn:
            return conn.execute(
                """
                SELECT id, text, image_file_id, button_text, button_url, created_at, last_sent_at
                FROM offers
                WHERE is_active = 1
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def get_offer(self, offer_id: int) -> sqlite3.Row | None:
        """يعيد عرضاً واحداً إن كان ما زال فعالاً."""
        with self._connection() as conn:
            return conn.execute(
                """
                SELECT id, text, image_file_id, button_text, button_url, created_at
                FROM offers WHERE id = ? AND is_active = 1
                """,
                (offer_id,),
            ).fetchone()

    def deactivate_offer(self, offer_id: int) -> bool:
        """يحذف العرض من الاستخدام بطريقة آمنة دون حذف السجل التاريخي."""
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE offers SET is_active = 0 WHERE id = ? AND is_active = 1",
                (offer_id,),
            )
            return cursor.rowcount > 0

    def get_next_offer(self) -> sqlite3.Row | None:
        """يختار العرض الأقل إرسالاً حديثاً ليُناوب البوت بين العروض."""
        with self._connection() as conn:
            offer = conn.execute(
                """
                SELECT id, text, image_file_id, button_text, button_url
                FROM offers
                WHERE is_active = 1
                ORDER BY
                    CASE WHEN last_sent_at IS NULL THEN 0 ELSE 1 END,
                    last_sent_at ASC,
                    id ASC
                LIMIT 1
                """
            ).fetchone()
            if offer is not None:
                conn.execute(
                    "UPDATE offers SET last_sent_at = ? WHERE id = ?",
                    (self._utc_now_iso(), offer["id"]),
                )
            return offer

    def get_active_user_ids(self) -> list[int]:
        """يعيد معرفات المستخدمين الذين يستطيع البوت مراسلتهم."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT user_id FROM users WHERE is_active = 1 ORDER BY user_id"
            ).fetchall()
            return [int(row["user_id"]) for row in rows]

    def record_offer_delivery(
        self,
        offer_id: int,
        user_id: int,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """يوثق نتيجة محاولة إرسال عرض للمراجعة لاحقاً."""
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO offer_deliveries
                    (offer_id, user_id, delivered_at, status, error_message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (offer_id, user_id, self._utc_now_iso(), status, error_message),
            )

    def create_payment_and_extend_subscription(
        self,
        *,
        user_id: int,
        telegram_payment_charge_id: str,
        invoice_payload: str,
        currency: str,
        total_amount: int,
        subscription_days: int,
    ) -> tuple[bool, datetime]:
        """يحفظ الدفعة مرة واحدة فقط ويمدد اشتراك المستخدم في معاملة واحدة.

        تعيد الدالة زوجاً من: هل كانت الدفعة جديدة، وتاريخ انتهاء الاشتراك.
        """
        now = self._utc_now()
        with self._connection() as conn:
            existing_payment = conn.execute(
                "SELECT 1 FROM payments WHERE telegram_payment_charge_id = ?",
                (telegram_payment_charge_id,),
            ).fetchone()
            user = conn.execute(
                "SELECT subscription_until FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()

            # في الظروف الطبيعية يكون المستخدم مسجلاً عند /start، لكن نضمن وجوده هنا.
            if user is None:
                now_iso = now.isoformat()
                conn.execute(
                    """
                    INSERT INTO users (user_id, username, full_name, first_seen_at, last_seen_at)
                    VALUES (?, NULL, 'مستخدم تلغرام', ?, ?)
                    """,
                    (user_id, now_iso, now_iso),
                )
                user = {"subscription_until": None}

            current_until: datetime | None = None
            if user["subscription_until"]:
                current_until = datetime.fromisoformat(user["subscription_until"])

            if existing_payment:
                return False, current_until or now

            # إن كان لديه اشتراك نشط نضيف الشهر بعد نهايته، وإلا نبدأ من الآن.
            base = current_until if current_until and current_until > now else now
            subscription_until = base + timedelta(days=subscription_days)

            conn.execute(
                """
                INSERT INTO payments
                    (telegram_payment_charge_id, user_id, invoice_payload, currency, total_amount, paid_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_payment_charge_id,
                    user_id,
                    invoice_payload,
                    currency,
                    total_amount,
                    now.isoformat(),
                ),
            )
            conn.execute(
                """
                UPDATE users
                SET subscription_until = ?, access_revoked = 0, is_active = 1, last_seen_at = ?
                WHERE user_id = ?
                """,
                (subscription_until.isoformat(), now.isoformat(), user_id),
            )
            return True, subscription_until

    def get_subscription_until(self, user_id: int) -> datetime | None:
        """يعيد نهاية الاشتراك أو None إن لم يوجد اشتراك."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT subscription_until FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not row or not row["subscription_until"]:
                return None
            return datetime.fromisoformat(row["subscription_until"])

    def has_active_subscription(self, user_id: int) -> bool:
        """يتحقق من صلاحية الاشتراك بالنسبة إلى الوقت العالمي الحالي."""
        expires_at = self.get_subscription_until(user_id)
        return bool(expires_at and expires_at > self._utc_now())

    def get_expired_users_pending_revocation(self) -> list[int]:
        """يعيد المشتركين الذين انتهت صلاحيتهم ولم يُسحب دخولهم بعد."""
        now = self._utc_now_iso()
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT user_id
                FROM users
                WHERE subscription_until IS NOT NULL
                  AND subscription_until <= ?
                  AND access_revoked = 0
                """,
                (now,),
            ).fetchall()
            return [int(row["user_id"]) for row in rows]

    def mark_access_revoked(self, user_id: int) -> None:
        """يعلم أن دخول القناة سُحب بعد انتهاء الاشتراك."""
        with self._connection() as conn:
            conn.execute("UPDATE users SET access_revoked = 1 WHERE user_id = ?", (user_id,))

    def get_stats(self) -> dict[str, int]:
        """يجمع إحصاءات موجزة تعرض في لوحة الإدارة."""
        now = self._utc_now_iso()
        with self._connection() as conn:
            total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            active_users = conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_active = 1"
            ).fetchone()[0]
            active_subscribers = conn.execute(
                "SELECT COUNT(*) FROM users WHERE subscription_until > ?", (now,)
            ).fetchone()[0]
            active_offers = conn.execute(
                "SELECT COUNT(*) FROM offers WHERE is_active = 1"
            ).fetchone()[0]
            payments_count = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
            stars_total = conn.execute(
                "SELECT COALESCE(SUM(total_amount), 0) FROM payments WHERE currency = 'XTR'"
            ).fetchone()[0]
        return {
            "total_users": int(total_users),
            "active_users": int(active_users),
            "active_subscribers": int(active_subscribers),
            "active_offers": int(active_offers),
            "payments_count": int(payments_count),
            "stars_total": int(stars_total),
        }

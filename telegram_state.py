"""Private, durable state for Telegram searches and their notifications."""

import datetime as dt
import json
import sqlite3
import time
from dataclasses import asdict

from luxmed_sniper import WARSAW
from telegram_requests import Criteria, Preferences


def next_search_time(timestamp: int) -> int:
    local = dt.datetime.fromtimestamp(timestamp, WARSAW)
    if dt.time(6) <= local.time() < dt.time(22, 1):
        return timestamp
    opening = local.replace(hour=6, minute=0, second=0, microsecond=0)
    if local.hour >= 6:
        opening += dt.timedelta(days=1)
    return int(opening.timestamp())


class SearchState:
    def __init__(self, path, *, clock=time.time):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA secure_delete = ON;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (
                patient TEXT PRIMARY KEY, next_attempt INTEGER NOT NULL DEFAULT 0, suspended INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, patient TEXT NOT NULL, service TEXT NOT NULL,
                city_id INTEGER NOT NULL, city TEXT NOT NULL, polish INTEGER NOT NULL,
                telephone INTEGER NOT NULL, next_due INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, search_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS message_searches (
                message_id INTEGER PRIMARY KEY, search_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS search_results (
                search_id INTEGER PRIMARY KEY, slots TEXT NOT NULL, shown INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS booking_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id INTEGER NOT NULL, option_number INTEGER NOT NULL,
                patient TEXT NOT NULL, slot_key TEXT NOT NULL,
                status TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0,
                reason TEXT, reservation_id INTEGER,
                UNIQUE (search_id, option_number)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS outstanding_booking_slot
                ON booking_requests (patient, slot_key) WHERE status NOT IN ('stopped', 'ended');
        """)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(searches)")}
            for name, declaration in (
                ("last_checked", "INTEGER"),
                ("last_result", "TEXT"),
                ("first_result_sent", "INTEGER NOT NULL DEFAULT 0"),
                ("preferences", "TEXT NOT NULL DEFAULT '{}'"),
                ("display_number", "INTEGER"),
                ("excluded_slots", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE searches ADD COLUMN {name} {declaration}")
            self.db.execute("UPDATE searches SET display_number = id WHERE display_number IS NULL")
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS active_search_number ON searches (display_number) WHERE status = 'active'"
            )
            if not self.get_setting("search_schedule_per_search"):
                self.db.execute(
                    """UPDATE searches SET next_due = min(next_due, ?)
                       WHERE status = 'active' AND last_checked IS NULL AND first_result_sent = 0""",
                    (next_search_time(int(clock())),),
                )
                self.set_setting("search_schedule_per_search", "1")

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))

    def queue_message(self, text: str, search_id: int | None = None) -> None:
        self.db.execute("INSERT INTO outbox (text, search_id) VALUES (?, ?)", (text, search_id))

    def next_message(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM outbox ORDER BY id LIMIT 1").fetchone()

    def sent(self, message: sqlite3.Row, telegram_id: int) -> None:
        with self.db:
            if message["search_id"] is not None:
                self.db.execute("INSERT OR IGNORE INTO message_searches VALUES (?, ?)", (telegram_id, message["search_id"]))
            self.db.execute("DELETE FROM outbox WHERE id = ?", (message["id"],))

    def search_for_message(self, message_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            """SELECT searches.id, searches.display_number FROM searches
               JOIN message_searches ON searches.id = message_searches.search_id
               WHERE message_id = ?""",
            (message_id,),
        ).fetchone()

    def add(self, criteria: Criteria, now: int, *, excluded_slots=(), not_before: int = 0) -> tuple[int, int, int, bool]:
        self.db.execute("INSERT OR IGNORE INTO accounts (patient) VALUES (?)", (criteria.patient,))
        account = self.db.execute("SELECT suspended FROM accounts WHERE patient = ?", (criteria.patient,)).fetchone()
        due = next_search_time(max(now, not_before))
        number = self.db.execute(
            """SELECT CASE WHEN EXISTS (SELECT 1 FROM searches WHERE status = 'active')
               THEN (SELECT display_number + 1 FROM searches ORDER BY id DESC LIMIT 1)
               ELSE 1 END""",
        ).fetchone()[0]
        row = self.db.execute(
            """INSERT INTO searches (patient, service, city_id, city, polish, telephone, next_due, preferences,
                                     display_number, excluded_slots)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (criteria.patient, criteria.service, criteria.city_id, criteria.city, 0, criteria.telephone,
             due, json.dumps(asdict(criteria.preferences), ensure_ascii=False), number, json.dumps(excluded_slots)),
        )
        return row.lastrowid, number, due, bool(account["suspended"])

    def active_by_number(self, number: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM searches WHERE status = 'active' AND display_number = ?", (number,),
        ).fetchone()

    def save_results(self, search_id: int, slots: list[dict]) -> None:
        self.db.execute("INSERT INTO search_results (search_id, slots) VALUES (?, ?)", (search_id, json.dumps(slots)))

    def mark_shown(self, search_id: int, shown: int) -> None:
        self.db.execute("UPDATE search_results SET shown = ? WHERE search_id = ?", (shown, search_id))

    def results(self, search_id: int | None = None) -> sqlite3.Row | None:
        if search_id is None:
            search_id = int(self.get_setting("latest_result", "0"))
        return self.db.execute(
            """SELECT searches.*, search_results.slots, search_results.shown
               FROM searches JOIN search_results ON searches.id = search_results.search_id
               WHERE searches.id = ?""",
            (search_id,),
        ).fetchone()

    def mark_continued(self, search_id: int) -> None:
        self.db.execute("UPDATE searches SET status = 'continued' WHERE id = ? AND status = 'found'", (search_id,))

    def request_booking(self, search_id: int, number: int, patient: str, slot_key: str):
        self.db.execute(
            """INSERT OR IGNORE INTO booking_requests (search_id, option_number, patient, slot_key, status)
               VALUES (?, ?, ?, ?, 'queued')""",
            (search_id, number, patient, slot_key),
        )
        return self.db.execute(
            """SELECT * FROM booking_requests WHERE patient = ? AND slot_key = ?
               ORDER BY status IN ('stopped', 'ended'), id DESC LIMIT 1""",
            (patient, slot_key),
        ).fetchone()

    def approve_booking(self, booking_id: int) -> bool:
        return self.db.execute(
            "UPDATE booking_requests SET approved = 1, status = 'queued' WHERE id = ? AND status = 'approval'",
            (booking_id,),
        ).rowcount == 1

    def claim_booking(self):
        with self.db:
            return self.db.execute(
                """UPDATE booking_requests SET status = 'checking'
                   WHERE id = (SELECT id FROM booking_requests WHERE status = 'queued' ORDER BY id LIMIT 1)
                   RETURNING *""",
            ).fetchone()

    def booking_status(self, booking_id: int, status: str, *, reason=None, reservation_id=None) -> None:
        self.db.execute(
            "UPDATE booking_requests SET status = ?, reason = ?, reservation_id = ? WHERE id = ?",
            (status, reason, reservation_id, booking_id),
        )

    def booking_recovery(self):
        return self.db.execute(
            "SELECT * FROM booking_requests WHERE status IN ('checking', 'submitting', 'verifying')",
        ).fetchall()

    def stop_booking(self, booking_id: int) -> bool:
        return self.db.execute(
            "UPDATE booking_requests SET status = 'stopped' WHERE id = ? AND status IN ('queued', 'approval')",
            (booking_id,),
        ).rowcount == 1

    def account_paused(self, patient: str) -> bool:
        return bool(self.db.execute("SELECT suspended FROM accounts WHERE patient = ?", (patient,)).fetchone()[0])

    def unresolved_booking(self, patient: str) -> bool:
        return self.db.execute(
            """SELECT 1 FROM booking_requests WHERE patient = ? AND status IN ('unknown', 'verifying') LIMIT 1""",
            (patient,),
        ).fetchone() is not None

    def acknowledge_unverified_booking(self, booking_id: int) -> bool:
        return self.db.execute(
            "UPDATE booking_requests SET status = 'reviewed' WHERE id = ? AND status = 'unknown'",
            (booking_id,),
        ).rowcount == 1

    def active(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT searches.*, accounts.suspended, next_due AS next_check
               FROM searches JOIN accounts USING (patient)
               WHERE status = 'active' ORDER BY id""",
        ).fetchall()

    def expired(self, today: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM searches WHERE status = 'active' AND json_extract(preferences, '$.last_date') < ?",
            (today,),
        ).fetchall()

    def revise(self, search_id: int, criteria: Criteria, now: int) -> tuple[int, bool]:
        account = self.db.execute("SELECT suspended FROM accounts WHERE patient = ?", (criteria.patient,)).fetchone()
        due = next_search_time(now)
        self.db.execute(
            """UPDATE searches SET service = ?, city_id = ?, city = ?, polish = ?, telephone = ?,
                   next_due = ?, preferences = ?, last_checked = NULL, last_result = NULL, first_result_sent = 0
               WHERE id = ? AND status = 'active' AND patient = ?""",
            (criteria.service, criteria.city_id, criteria.city, 0, criteria.telephone,
             due, json.dumps(asdict(criteria.preferences), ensure_ascii=False), search_id, criteria.patient),
        )
        return due, bool(account["suspended"])

    @staticmethod
    def criteria(row: sqlite3.Row) -> Criteria:
        return Criteria(
            row["patient"], row["service"], row["city_id"], row["city"], bool(row["telephone"]),
            Preferences(**json.loads(row["preferences"])),
        )

    def record_result(self, search_id: int, now: int, result: str) -> int | None:
        row = self.db.execute(
            """UPDATE searches SET last_checked = ?, last_result = ?
               WHERE id = ? AND status = 'active' RETURNING next_due""",
            (now, result, search_id),
        ).fetchone()
        return row["next_due"] if row else None

    def mark_first_result(self, search_id: int) -> bool:
        cursor = self.db.execute(
            "UPDATE searches SET first_result_sent = 1 WHERE id = ? AND status = 'active' AND first_result_sent = 0",
            (search_id,),
        )
        return cursor.rowcount == 1

    def finish(self, search_id: int, status: str) -> bool:
        cursor = self.db.execute(
            "UPDATE searches SET status = ? WHERE id = ? AND status = 'active'", (status, search_id),
        )
        return cursor.rowcount == 1

    def suspend(self, patient: str) -> None:
        self.db.execute("UPDATE accounts SET suspended = 1 WHERE patient = ?", (patient,))

    def resume(self, patient: str) -> bool:
        row = self.db.execute("UPDATE accounts SET suspended = 0 WHERE patient = ? AND suspended = 1", (patient,))
        return row.rowcount == 1

    def claim_batch(self, now: int) -> list[sqlite3.Row]:
        if next_search_time(now) != now:
            return []
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            patient = self.db.execute(
                """SELECT patient FROM searches JOIN accounts USING (patient)
                   WHERE status = 'active' AND next_due <= ? AND suspended = 0
                   ORDER BY next_due, id LIMIT 1""",
                (now,),
            ).fetchone()
            if patient is None:
                return []
            rows = self.db.execute(
                """SELECT * FROM searches WHERE patient = ? AND status = 'active' AND next_due <= ?
                   ORDER BY next_due, id""",
                (patient["patient"], now),
            ).fetchall()
            retry_at = next_search_time(now + 3600)
            self.db.executemany("UPDATE searches SET next_due = ? WHERE id = ?", ((retry_at, row["id"]) for row in rows))
            return rows

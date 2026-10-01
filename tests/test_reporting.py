"""Проверки периодов отчёта и SMTP без сетевых запросов."""

import sqlite3
import ssl
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from reporting import MOSCOW, ReportSettings, load_report_settings, report_content, send_due_report, send_report_email
from storage import CatalogDB


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "gudd.sqlite3"
        self.db = CatalogDB(self.path)
        self.db.upsert_user({"id": 100, "username": "buyer", "first_name": "Иван", "last_name": "Иванов"})
        self.db.upsert_user({"id": 101, "username": "guest", "first_name": "Анна"})
        with self.db.conn:
            self.db.conn.executemany(
                """INSERT INTO user_events(telegram_id, event_type, property_id, created_at)
                   VALUES (?, 'property_opened', ?, ?)""",
                [
                    (100, 1, "2026-10-01 06:59:59"),  # до четверга 10:00 МСК
                    (100, 1, "2026-10-01 07:00:00"),
                    (100, 1, "2026-10-02 12:00:00"),
                    (100, 2, "2026-10-05 06:59:59"),
                    (101, 2, "2026-10-05 07:00:00"),  # следующий отчёт
                    (101, 2, "2026-10-08 06:59:59"),
                    (101, 2, "2026-10-08 07:00:00"),  # ещё следующий отчёт
                ],
            )

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_report_periods_do_not_overlap_or_include_phone(self):
        start = datetime(2026, 10, 1, 10, tzinfo=MOSCOW)
        monday = datetime(2026, 10, 5, 10, tzinfo=MOSCOW)
        thursday = datetime(2026, 10, 8, 10, tzinfo=MOSCOW)
        subject, body = report_content(self.db, start, monday)
        self.assertIn("01.10 10:00–05.10.2026 10:00", subject)
        self.assertIn("Просмотров объектов: 3", body)
        self.assertIn("Уникальных посетителей: 1", body)
        self.assertIn("2 просмотра, 1 посетитель", body)
        self.assertNotIn("телефон", body)
        self.assertEqual(sum("Telegram ID 100" in line for line in body.splitlines()), 2)
        _, next_body = report_content(self.db, monday, thursday)
        self.assertIn("Просмотров объектов: 2", next_body)
        self.assertIn("Уникальных посетителей: 1", next_body)
        self.assertNotIn("Telegram ID 100", next_body)

    def test_monday_thursday_due_time_retry_and_catchup(self):
        settings = ReportSettings("smtp.mail.ru", 465, "sender@mail.ru", "unused", "sender@mail.ru", "gudd22@mail.ru")
        sent = []
        sender = lambda config, subject, body: sent.append((subject, body))
        self.db.mark_report_sent("2026-10-01", settings.recipient)
        before = datetime(2026, 10, 5, 9, 59, tzinfo=MOSCOW)
        monday = datetime(2026, 10, 5, 10, tzinfo=MOSCOW)
        self.assertIsNone(send_due_report(self.db, settings, before, sender))

        def fail_once(config, subject, body):
            raise OSError("SMTP недоступен")

        with self.assertRaises(OSError):
            send_due_report(self.db, settings, monday, fail_once)
        self.assertEqual(self.db.last_report_period_end(), "2026-10-01")
        self.assertEqual(str(send_due_report(self.db, settings, monday, sender)), "2026-10-05")
        self.db.close()
        self.db = CatalogDB(self.path)
        self.assertIsNone(send_due_report(self.db, settings, monday, sender))
        self.assertIsNone(send_due_report(self.db, settings, datetime(2026, 10, 8, 9, 59, tzinfo=MOSCOW), sender))
        self.assertEqual(str(send_due_report(self.db, settings, datetime(2026, 10, 8, 10, tzinfo=MOSCOW), sender)), "2026-10-08")
        after_downtime = datetime(2026, 10, 15, 10, tzinfo=MOSCOW)
        self.assertEqual(str(send_due_report(self.db, settings, after_downtime, sender)), "2026-10-12")
        self.assertEqual(str(send_due_report(self.db, settings, after_downtime, sender)), "2026-10-15")
        self.assertIsNone(send_due_report(self.db, settings, after_downtime, sender))
        self.assertEqual(len(sent), 4)

    def test_incomplete_smtp_settings_are_rejected(self):
        values = {}
        read = lambda env_file, name: values.get(name, "")
        self.assertIsNone(load_report_settings(None, read))
        values["SMTP_HOST"] = "smtp.mail.ru"
        with self.assertRaisesRegex(ValueError, "SMTP_USER"):
            load_report_settings(None, read)
        values.update(SMTP_USER="sender@mail.ru", SMTP_PASSWORD="app-password")
        with self.assertRaisesRegex(ValueError, "REPORT_TO"):
            load_report_settings(None, read)
        values["REPORT_TO"] = "gudd22@mail.ru"
        settings = load_report_settings(None, read)
        self.assertEqual(settings.recipient, "gudd22@mail.ru")
        self.assertEqual(settings.port, 465)

    def test_old_database_migrates_without_losing_phone_history(self):
        old_path = Path(self.temp.name) / "version2.sqlite3"
        with sqlite3.connect(old_path) as conn:
            conn.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, phone_number TEXT)")
            conn.execute("CREATE TABLE user_events (id INTEGER PRIMARY KEY, telegram_id INTEGER, event_type TEXT, property_id INTEGER, created_at TEXT)")
            conn.execute("INSERT INTO users VALUES (100, '+79991234567')")
            conn.execute("INSERT INTO user_events VALUES (1, 100, 'phone_shared', 1, '2026-10-01 12:00:00')")
            conn.execute("PRAGMA user_version = 2")
        migrated = CatalogDB(old_path)
        try:
            self.assertEqual(migrated.conn.execute("SELECT phone_number FROM user_events WHERE id = 1").fetchone()[0], "+79991234567")
            self.assertEqual(migrated.conn.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertIsNone(migrated.last_report_period_end())
        finally:
            migrated.close()

    def test_smtp_delivery_uses_tls_and_requested_recipient(self):
        settings = ReportSettings("smtp.mail.ru", 465, "sender@mail.ru", "secret", "sender@mail.ru", "gudd22@mail.ru")
        with patch("reporting.smtplib.SMTP_SSL") as smtp_class:
            send_report_email(settings, "Сводка", "Посетителей: 2")
        args, kwargs = smtp_class.call_args
        self.assertEqual(args, ("smtp.mail.ru", 465))
        self.assertEqual(kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.login.assert_called_once_with("sender@mail.ru", "secret")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "gudd22@mail.ru")
        self.assertIn("Посетителей: 2", message.get_content())


if __name__ == "__main__":
    unittest.main()

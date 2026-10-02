"""Проверки периодов отчёта и SMTP без сетевых запросов."""

import io
import sqlite3
import ssl
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from reporting import MOSCOW, ReportSettings, load_report_settings, report_content, send_due_report, send_report_email
from storage import CatalogDB
from verify import send_test_email


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
        subject, body, html_body = report_content(self.db, start, monday)
        self.assertIn("01.10 10:00–05.10.2026 10:00", subject)
        self.assertIn("Просмотров объектов: 3", body)
        self.assertIn("Уникальных посетителей: 1", body)
        self.assertIn("Квартира у набережной: 2 просмотра, 1 посетитель", body)
        self.assertIn("Квартира у набережной · Иван Иванов · @buyer", body)
        self.assertNotRegex(body, r"(?m)^#\d+\b")
        self.assertNotIn("телефон", body)
        self.assertNotIn("Telegram ID", body)
        self.assertIn("<table", html_body)
        self.assertIn('href="https://t.me/buyer"', html_body)
        self.assertNotIn("Telegram ID", html_body)
        self.assertNotIn("телефон", html_body)
        self.assertEqual(sum("Квартира у набережной · Иван Иванов" in line for line in body.splitlines()), 1)
        _, next_body, _ = report_content(self.db, monday, thursday)
        self.assertIn("Просмотров объектов: 2", next_body)
        self.assertIn("Уникальных посетителей: 1", next_body)
        self.assertNotIn("Иван Иванов", next_body)

    def test_html_escapes_card_and_profile_text_and_handles_missing_nick(self):
        self.db.update_property(1, "title", "Дом <у реки>")
        self.db.upsert_user({
            "id": 100, "username": 'buyer"><img src=x>',
            "first_name": "<Иван>", "last_name": "Иванов",
        })
        start = datetime(2026, 10, 1, 10, tzinfo=MOSCOW)
        monday = datetime(2026, 10, 5, 10, tzinfo=MOSCOW)
        _, _, html_body = report_content(self.db, start, monday)
        self.assertIn("Дом &lt;у реки&gt;", html_body)
        self.assertIn("&lt;Иван&gt;", html_body)
        self.assertNotIn("<img", html_body)
        self.assertNotIn("https://t.me/buyer", html_body)

        self.db.upsert_user({"id": 101, "first_name": "Анна"})
        _, _, next_html = report_content(self.db, monday, datetime(2026, 10, 8, 10, tzinfo=MOSCOW))
        self.assertIn("не указан", next_html)
        self.assertNotIn("https://t.me/guest", next_html)

    def test_deleted_card_keeps_its_views_without_exposing_internal_id(self):
        self.db.delete_property(1)
        _, body, html_body = report_content(
            self.db, datetime(2026, 10, 1, 10, tzinfo=MOSCOW),
            datetime(2026, 10, 5, 10, tzinfo=MOSCOW),
        )
        self.assertIn("(объект удалён): 2 просмотра, 1 посетитель", body)
        self.assertIn("(объект удалён)", html_body)
        self.assertNotIn("Telegram ID", body + html_body)
        self.assertNotIn("#1", body)
        self.assertNotRegex(html_body, r">\s*#1(?:\s|<)")

    def test_monday_thursday_due_time_retry_and_catchup(self):
        settings = ReportSettings("smtp.mail.ru", 465, "sender@mail.ru", "unused", "sender@mail.ru", "gudd22@mail.ru")
        sent = []
        sender = lambda config, subject, body, html_body: sent.append((subject, body, html_body))
        self.db.mark_report_sent("2026-10-01", settings.recipient)
        before = datetime(2026, 10, 5, 9, 59, tzinfo=MOSCOW)
        monday = datetime(2026, 10, 5, 10, tzinfo=MOSCOW)
        self.assertIsNone(send_due_report(self.db, settings, before, sender))

        def fail_once(config, subject, body, html_body):
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
        subject, body, html_body = report_content(
            self.db, datetime(2026, 10, 1, 10, tzinfo=MOSCOW),
            datetime(2026, 10, 5, 10, tzinfo=MOSCOW),
        )
        with patch("reporting.smtplib.SMTP_SSL") as smtp_class:
            send_report_email(settings, subject, body, html_body)
        args, kwargs = smtp_class.call_args
        self.assertEqual(args, ("smtp.mail.ru", 465))
        self.assertEqual(kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.login.assert_called_once_with("sender@mail.ru", "secret")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "gudd22@mail.ru")
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        self.assertIn("Просмотров объектов: 3", message.get_body(preferencelist=("plain",)).get_content())
        self.assertIn('href="https://t.me/buyer"', message.get_body(preferencelist=("html",)).get_content())

    def test_test_email_uses_html_without_marking_a_regular_delivery(self):
        settings = ReportSettings("smtp.mail.ru", 465, "sender@mail.ru", "secret", "sender@mail.ru", "gudd22@mail.ru")
        with patch("verify.load_report_settings", return_value=settings), patch("verify.send_report_email") as deliver:
            with redirect_stdout(io.StringIO()):
                send_test_email(self.path, Path(self.temp.name) / ".env")
        args = deliver.call_args.args
        self.assertTrue(args[1].startswith("[ТЕСТ]"))
        self.assertIn("<table", args[3])
        self.assertIsNone(self.db.last_report_period_end())


if __name__ == "__main__":
    unittest.main()

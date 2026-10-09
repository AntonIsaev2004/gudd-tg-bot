"""Проверки текстовых цен и переноса действующей базы с числовыми ценами."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admin import parse_value
from bot import detail_caption
from import_catalog import validated_catalog
from pricing import monthly_price, price
from storage import CatalogDB


class TextPriceTests(unittest.TestCase):
    def numeric_database(self, path):
        with patch.object(CatalogDB, "_migrate_text_prices", return_value=None):
            db = CatalogDB(path)
        batch_id, items = validated_catalog()
        db.import_catalog(batch_id, items)
        db.upsert_user({"id": 100, "first_name": "Посетитель"})
        db.record_event(100, "property_opened", db.list_properties()[0].id)
        db.mark_report_sent("2026-10-08 07:00:00", "example@example.com")
        self.assertEqual(db.conn.execute("PRAGMA user_version").fetchone()[0], 6)
        return db, items

    def test_migration_preserves_data_links_and_deleted_id_sequence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.sqlite3"
            old, items = self.numeric_database(path)
            first = old.list_properties()[0]
            old.set_active(first.id, False)
            old.update_property(first.id, "sale_price", None)
            deleted_id = old.create_property(items[0], ["deleted_photo"])
            old.record_event(100, "property_opened", deleted_id)
            old.delete_property(deleted_id)
            tables = ("properties", "property_photos", "users", "user_events", "catalog_imports", "report_deliveries")
            before = {table: [dict(row) for row in old.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                      for table in tables}
            for row in before["properties"]:
                self.assertIsInstance(row["price"], int)
                row["price"] = str(row["price"])
                if row["sale_price"] is not None:
                    row["sale_price"] = str(row["sale_price"])
            old.close()

            db = CatalogDB(path)
            try:
                after = {table: [dict(row) for row in db.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                         for table in tables}
                self.assertEqual(before, after)
                columns = {row["name"]: row["type"] for row in db.conn.execute("PRAGMA table_info(properties)")}
                self.assertEqual((columns["price"], columns["sale_price"]), ("TEXT", "TEXT"))
                self.assertEqual(db.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(db.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(db.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertFalse(db.is_active(first.id))
                self.assertIsNone(db.get_property(first.id, active_only=False).sale_price)
                self.assertIsNotNone(db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_property_source_row'").fetchone())
                new_id = db.create_property(dict(items[0], price="71 тыс.", sale_price="10,1 млн."), ["new_photo"])
                self.assertGreater(new_id, deleted_id)
                self.assertEqual(db.get_property(new_id).sale_price, "10,1 млн.")
            finally:
                db.close()
            db = CatalogDB(path)
            try:
                self.assertEqual(db.get_property(new_id).price, "71 тыс.")
                db.delete_property(new_id)
                self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM property_photos WHERE property_id = ?", (new_id,)).fetchone()[0], 0)
            finally:
                db.close()

    def test_failed_migration_rolls_back_schema_and_data(self):
        with tempfile.TemporaryDirectory() as temp:
            db, _ = self.numeric_database(Path(temp) / "rollback.sqlite3")
            try:
                db.conn.execute("PRAGMA foreign_keys = OFF")
                db.conn.execute("INSERT INTO property_photos(property_id, media, position) VALUES (999999, 'orphan', 0)")
                db.conn.commit()
                db.conn.execute("PRAGMA foreign_keys = ON")
                before = [tuple(row) for row in db.conn.execute("SELECT * FROM properties ORDER BY id")]
                with self.assertRaises(sqlite3.IntegrityError):
                    db._migrate_text_prices()
                self.assertEqual(db.conn.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(db.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(before, [tuple(row) for row in db.conn.execute("SELECT * FROM properties ORDER BY id")])
                self.assertIsNone(db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'properties_new'").fetchone())
                with db.conn:
                    db.conn.execute("DELETE FROM property_photos WHERE property_id = 999999")
                db._migrate_text_prices()
                self.assertEqual(db.conn.execute("PRAGMA user_version").fetchone()[0], 7)
            finally:
                db.close()

    def test_text_input_and_currency_suffixes(self):
        for field in ("price", "sale_price"):
            for text in ("10,1 млн.", "от 10 млн.", "71 тыс. ₽/мес.", "по запросу"):
                self.assertEqual(parse_value(field, text), text)
            for invalid in ("", " " * 5, "x" * 51, "10\nмлн."):
                with self.assertRaises(ValueError):
                    parse_value(field, invalid)
        self.assertEqual(price("10,1 млн."), "10,1 млн. ₽")
        self.assertEqual(price("от 10 млн. ₽"), "от 10 млн. ₽")
        self.assertEqual(monthly_price("71 тыс."), "71 тыс. ₽/мес.")
        self.assertEqual(monthly_price("71 тыс. ₽/мес. с НДС", with_vat=True), "71 тыс. ₽/мес. с НДС")
        self.assertEqual(monthly_price("по запросу", with_vat=True), "по запросу")

    def test_price_text_is_escaped_in_card_html(self):
        with tempfile.TemporaryDirectory() as temp:
            db = CatalogDB(Path(temp) / "html.sqlite3")
            try:
                _, items = validated_catalog()
                item_id = db.create_property(dict(items[0], price="71 тыс. <b>&", sale_price="10 млн. <b>&"), [])
                item = db.get_property(item_id)
                self.assertIn("71 тыс. &lt;b&gt;&amp; ₽/мес.", detail_caption(item))
                self.assertIn("10 млн. &lt;b&gt;&amp; ₽", detail_caption(item, mode="sale"))
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()

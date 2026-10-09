"""Независимые подписи кнопок, админка и сохранение действующей базы при обновлении."""

import html
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from admin import AdminPanel, parse_value
from catalog_labels import compact_price, initial_button_text
from import_catalog import validated_catalog
from storage import CatalogDB
from test_workflows import FakeAPI, callback, message


class CatalogLabelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "labels.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def legacy_database(self):
        with patch.object(CatalogDB, "_migrate_button_texts", return_value=None):
            db = CatalogDB(self.path)
        batch_id, items = validated_catalog()
        with db.conn:
            for index, item in enumerate(items, start=1):
                db.conn.execute(
                    """INSERT INTO properties(id, title, price, sale_price, area, location, rooms,
                           teaser, description, features_json, source_row, sort_order, is_active)
                       VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, '[]', ?, ?, ?)""",
                    (index, item["button_address"], item["price"], item["sale_price"], item["area"],
                     item["location"], item["rooms"], item["description"], item["source_row"],
                     index * 10, int(index != 1)),
                )
                db.conn.execute("INSERT INTO property_photos(property_id, media, position) VALUES (?, 'uploaded_photo', 0)", (index,))
            db.conn.execute("UPDATE properties SET price = '72 тыс.', sale_price = '10250000', area = 29.8 WHERE id = 1")
            db.conn.execute("UPDATE properties SET title = 'Заголовок администратора' WHERE id = 2")
            db.conn.execute("INSERT INTO catalog_imports(batch_id) VALUES (?)", (batch_id,))
            db.conn.execute("INSERT INTO catalog_imports(batch_id) VALUES (?)", (f"{batch_id}:titles-without-address-v1",))
        db.upsert_user({"id": 100, "first_name": "Посетитель"})
        db.record_event(100, "property_opened", 1)
        db.record_event(100, "property_opened", 3)
        db.delete_property(3)
        db.mark_report_sent("2026-10-08 07:00:00", "example@example.com")
        self.assertEqual(db.conn.execute("PRAGMA user_version").fetchone()[0], 7)
        return db, batch_id, items

    def test_upgrade_uses_current_data_and_preserves_history_and_admin_changes(self):
        old, batch_id, items = self.legacy_database()
        tables = ("properties", "property_photos", "users", "user_events", "catalog_imports", "report_deliveries")
        before = {table: [dict(row) for row in old.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                  for table in tables}
        old.close()
        db = CatalogDB(self.path)
        try:
            after = {table: [dict(row) for row in db.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                     for table in tables}
            for row in after["properties"]:
                self.assertTrue(row.pop("rent_button_text"))
                self.assertTrue(row.pop("sale_button_text"))
            self.assertEqual(before, after)
            self.assertEqual(db.import_catalog(batch_id, items), (False, 0))
            db.apply_button_text_defaults(batch_id, items)
            db.apply_title_corrections(batch_id, items)
            item = db.get_property(1, active_only=False)
            self.assertEqual(item.rent_button_text, "Казань, Волочаевская, 4, пом.1 · 29,8 м² · 72 тыс. ₽/мес.")
            self.assertEqual(item.sale_button_text, "Казань, Волочаевская, 4, пом.1 · 29,8 м² · 10,25 млн. ₽")
            self.assertEqual(item.title, "Современный офис, пом. 1")
            self.assertEqual(db.get_property(2).title, "Заголовок администратора")
            self.assertEqual(item.photos, ("uploaded_photo",))
            self.assertFalse(db.is_active(1))
            self.assertIsNone(db.get_property(3, active_only=False))
            caption = bot.detail_caption(item)
            self.assertEqual(caption.splitlines()[0], f"<b>{item.title}</b>")
            self.assertIn(f"📍 {item.location}", caption)
            self.assertEqual(caption.count(item.location), 1)
            self.assertEqual(db.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.conn.execute("PRAGMA user_version").fetchone()[0], 8)
            db.update_property(1, "rent_button_text", "Своя кнопка аренды")
            db.update_property(1, "price", "99 тыс.")
            db.update_property(1, "location", "Новый полный адрес")
            db.update_property(1, "title", "Новый заголовок")
            db.delete_property(4)
        finally:
            db.close()
        db = CatalogDB(self.path)
        try:
            self.assertEqual(db.apply_button_text_defaults(batch_id, items), 0)
            self.assertEqual(db.apply_title_corrections(batch_id, items), 0)
            item = db.get_property(1, active_only=False)
            self.assertEqual(item.rent_button_text, "Своя кнопка аренды")
            self.assertEqual(item.sale_button_text, "Казань, Волочаевская, 4, пом.1 · 29,8 м² · 10,25 млн. ₽")
            self.assertEqual(item.title, "Новый заголовок")
            self.assertIsNone(db.get_property(4, active_only=False))
            for table in ("users", "user_events", "report_deliveries"):
                self.assertEqual(before[table], [dict(row) for row in db.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")])
        finally:
            db.close()

    def test_failed_upgrade_rolls_back_added_columns(self):
        db, _, _ = self.legacy_database()
        try:
            before = [tuple(row) for row in db.conn.execute("SELECT * FROM properties ORDER BY id")]
            with patch("storage.initial_button_text", side_effect=ValueError("test failure")), self.assertRaises(ValueError):
                db._migrate_button_texts()
            columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(properties)")}
            self.assertNotIn("rent_button_text", columns)
            self.assertNotIn("sale_button_text", columns)
            self.assertEqual(db.conn.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(before, [tuple(row) for row in db.conn.execute("SELECT * FROM properties ORDER BY id")])
            db._migrate_button_texts()
            self.assertEqual(db.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            db.close()

    def test_admin_edits_exact_button_text_without_changing_card_or_other_mode(self):
        db = CatalogDB(self.path)
        try:
            _, items = validated_catalog()
            item_id = db.create_property(items[0], ["photo"])
            original = db.get_property(item_id)
            api = FakeAPI()
            admin = AdminPanel(api, db, {42}, bot.send_detail)
            admin.edit_menu(42, item_id)
            buttons = api.calls[-1][1]["reply_markup"]["inline_keyboard"]
            self.assertTrue(any(button["text"] == "Заголовок" for row in buttons for button in row))
            self.assertTrue(any(button["text"] == "Текст кнопки в каталоге" for row in buttons for button in row))
            admin.handle_callback(callback(43, f"adm:buttontexts:{item_id}"))
            self.assertIn("Нет доступа", api.calls[-1][1]["text"])
            admin.handle_callback(callback(42, f"adm:buttontexts:{item_id}"))
            self.assertIn(original.sale_button_text, api.calls[-1][1]["text"])
            label = "Офис <у метро> & рядом — по запросу"
            admin.handle_callback(callback(42, f"adm:field:{item_id}:rent_button_text"))
            admin.handle_message(message(42, label))
            item = db.get_property(item_id)
            self.assertEqual(item.rent_button_text, label)
            self.assertEqual(item.sale_button_text, original.sale_button_text)
            self.assertEqual((item.title, item.location, item.price, item.area),
                             (original.title, original.location, original.price, original.area))
            self.assertEqual(bot.catalog(db)[1]["inline_keyboard"][0][0]["text"], label)
            self.assertEqual(bot.catalog(db, "sale")[1]["inline_keyboard"][0][0]["text"], original.sale_button_text)
            self.assertIn(html.escape(label), api.calls[-1][1]["text"])
            admin.handle_callback(callback(42, f"adm:preview:{item_id}"))
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM user_events").fetchone()[0], 0)
        finally:
            db.close()

    def test_initial_labels_and_input_validation(self):
        self.assertEqual(compact_price("89880000"), "89,88 млн.")
        self.assertEqual(compact_price("73 700"), "73,7 тыс.")
        self.assertEqual(compact_price("10,1 млн."), "10,1 млн.")
        self.assertEqual(compact_price("по запросу"), "по запросу")
        for field in ("rent_button_text", "sale_button_text"):
            self.assertEqual(parse_value(field, " Только мой текст "), "Только мой текст")
            for invalid in ("", "x" * 101, "два\nряда", "таб\tтекст"):
                with self.assertRaises(ValueError):
                    parse_value(field, invalid)
        _, items = validated_catalog()
        for item in items:
            for field, mode, amount in (("rent_button_text", "rent", item["price"]),
                                         ("sale_button_text", "sale", item["sale_price"])):
                self.assertEqual(item[field], initial_button_text(item["button_address"], item["area"], amount, mode=mode))
                self.assertLessEqual(len(item[field]), 100)


if __name__ == "__main__":
    unittest.main()

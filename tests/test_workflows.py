"""Сквозные проверки каталога, SQLite и админки без запросов к Telegram."""

import tempfile
import unittest
import sqlite3
import json
from unittest.mock import patch
from io import BytesIO, StringIO
from contextlib import redirect_stdout
from pathlib import Path

import bot
from admin import AdminPanel, STEPS
from storage import CatalogDB
from import_catalog import validated_catalog


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.next_id = 1

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "sendMediaGroup":
            result = [{"message_id": self.next_id + i} for i in range(len(params["media"]))]
            self.next_id += len(result)
            return result
        if method in ("sendMessage", "sendPhoto"):
            result = {"message_id": self.next_id}
            self.next_id += 1
            return result
        return True


def user(user_id):
    return {
        "id": user_id, "username": f"u{user_id}", "first_name": "Имя",
        "last_name": "Фамилия", "language_code": "ru",
    }


def message(user_id, text=None, photo=None, contact=None):
    result = {"chat": {"id": user_id, "type": "private"}, "from": user(user_id)}
    if text is not None:
        result["text"] = text
    if photo is not None:
        result["photo"] = [{"file_id": photo}]
    if contact is not None:
        result["contact"] = contact
    return result


def callback(user_id, data, message_id=1):
    return {
        "id": f"query-{user_id}-{data}", "from": user(user_id), "data": data,
        "message": {"chat": {"id": user_id, "type": "private"}, "message_id": message_id},
    }


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "gudd.sqlite3"
        self.db = CatalogDB(self.path)
        self.db.create_property({
            "title": "Офис первый", "price": 70_100, "sale_price": 10_100_000, "area": 36.3,
            "location": "Москва", "rooms": "Офис", "teaser": "",
            "description": "Помещение в аренду", "features": [],
        }, ["photo_1", "photo_2"])
        self.db.create_property({
            "title": "Офис второй", "price": 90_000, "area": 50,
            "location": "Москва", "rooms": "Офис", "teaser": "",
            "description": "Помещение в аренду", "features": [],
        }, ["photo_3"])
        self.api = FakeAPI()
        self.admin = AdminPanel(self.api, self.db, {42}, bot.send_detail)
        bot.ACTIVE_DETAILS.clear()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()
        bot.ACTIVE_DETAILS.clear()

    def test_catalog_user_tracking_and_persistence(self):
        self.assertEqual(len(self.db.list_properties()), 2)
        bot.handle_message(self.api, self.db, self.admin, message(100, "/start"))
        bot.handle_callback(self.api, self.db, self.admin, callback(100, "show:1"))
        row = self.db.conn.execute("SELECT * FROM users WHERE telegram_id = 100").fetchone()
        self.assertEqual((row["username"], row["first_name"], row["last_name"]),
                         ("u100", "Имя", "Фамилия"))
        self.assertEqual(row["last_property_id"], 1)
        self.assertTrue(row["first_seen_at"] and row["last_seen_at"])
        events = self.db.conn.execute("SELECT event_type, property_id FROM user_events ORDER BY id").fetchall()
        self.assertEqual([(event[0], event[1]) for event in events],
                         [("bot_started", None), ("property_opened", 1)])
        album = next(params for method, params in self.api.calls if method == "sendMediaGroup")
        self.assertIn("caption", album["media"][0])
        self.assertNotIn("caption", album["media"][1])
        self.db.close()
        self.db = CatalogDB(self.path)
        self.assertEqual(len(self.db.list_properties()), 2)
        self.assertEqual(self.db.conn.execute("SELECT last_property_id FROM users WHERE telegram_id = 100").fetchone()[0], 1)

    def test_manager_link_and_no_contact_collection(self):
        bot.handle_message(self.api, self.db, self.admin, message(100, "/start"))
        bot.handle_callback(self.api, self.db, self.admin, callback(100, "show:1"))
        controls_message = next(params for method, params in reversed(self.api.calls)
                                if method == "sendMessage" and "reply_markup" in params)
        controls = controls_message["reply_markup"]["inline_keyboard"]
        self.assertIn([{"text": "💬 Связаться с менеджером", "url": "https://t.me/gudd_manager"}], controls)
        self.assertFalse(any("request_contact" in button for row in controls for button in row))
        bot.handle_message(self.api, self.db, self.admin, message(100, contact={
            "user_id": 100, "phone_number": "+79991234567"}))
        self.assertIsNone(self.db.conn.execute(
            "SELECT phone_number FROM users WHERE telegram_id = 100").fetchone()[0])
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM user_events WHERE event_type = 'phone_shared'").fetchone()[0], 0)

    def test_help_lists_public_and_admin_commands(self):
        bot.handle_message(self.api, self.db, self.admin, message(100, "/help"))
        self.assertIn("/catalog", self.api.calls[-1][1]["text"])
        self.assertNotIn("/admin", self.api.calls[-1][1]["text"])
        bot.handle_message(self.api, self.db, self.admin, message(42, "/help"))
        self.assertIn("/admin", self.api.calls[-1][1]["text"])

    def test_version_one_database_migrates_without_losing_users(self):
        old_path = Path(self.temp.name) / "old.sqlite3"
        with sqlite3.connect(old_path) as conn:
            conn.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, username TEXT)")
            conn.execute("INSERT INTO users(telegram_id, username) VALUES (100, 'old_user')")
            conn.execute("PRAGMA user_version = 1")
        old_db = CatalogDB(old_path)
        try:
            row = old_db.conn.execute("SELECT username, phone_number, phone_shared_at FROM users").fetchone()
            self.assertEqual(row["username"], "old_user")
            self.assertIsNone(row["phone_number"])
            self.assertEqual(old_db.conn.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertIn("phone_number", [row["name"] for row in old_db.conn.execute("PRAGMA table_info(user_events)")])
        finally:
            old_db.close()

    def test_visualization_note_removed_from_existing_cards(self):
        with self.db.conn:
            self.db.conn.execute(
                "UPDATE properties SET features_json = ? WHERE id = 1",
                (json.dumps(["Окупаемость: 10 лет", "Часть изображений — визуализации"], ensure_ascii=False),),
            )
            self.db.conn.execute("PRAGMA user_version = 4")
        self.db.close()
        self.db = CatalogDB(self.path)
        item = self.db.get_property(1)
        self.assertEqual(item.features, ())
        self.assertNotIn("Часть изображений", bot.detail_caption(item))
        self.assertEqual(self.db.conn.execute("PRAGMA user_version").fetchone()[0], 6)

    def test_admin_access_edit_hide_and_order(self):
        bot.handle_message(self.api, self.db, self.admin, message(43, "/admin"))
        self.assertIn("Нет доступа", self.api.calls[-1][1]["text"])
        bot.handle_callback(self.api, self.db, self.admin, callback(43, "adm:delete:1"))
        bot.handle_callback(self.api, self.db, self.admin, callback(43, "adm:confirmdelete:1"))
        self.assertIsNotNone(self.db.get_property(1))
        bot.handle_message(self.api, self.db, self.admin, message(42, "/admin"))
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:field:1:title"))
        self.assertNotIn("на кнопке", self.api.calls[-1][1]["text"])
        bot.handle_message(self.api, self.db, self.admin, message(42, "Новый офис"))
        self.assertEqual(self.db.get_property(1).title, "Новый офис")
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:field:1:area"))
        bot.handle_message(self.api, self.db, self.admin, message(42, "82,5"))
        self.assertEqual(self.db.get_property(1).area, 82.5)
        self.assertIn("82,5 м²", bot.catalog(self.db)[1]["inline_keyboard"][0][0]["text"])
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:toggle:1"))
        self.assertIsNone(self.db.get_property(1))
        self.assertEqual(len(bot.catalog(self.db)[1]["inline_keyboard"]), 2)
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:toggle:1"))
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:down:1"))
        self.assertEqual([item.id for item in self.db.list_properties()][:2], [2, 1])

    def test_admin_creates_and_deletes_card_with_photos(self):
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:add"))
        answers = (
            "Тестовый офис", "95000", "11000000", "75", "Казань", "Офис",
            "Короткий текст", "Подробное описание", "Парковка, Балкон",
        )
        self.assertEqual(len(answers), len(STEPS))
        for answer in answers:
            bot.handle_message(self.api, self.db, self.admin, message(42, answer))
        bot.handle_message(self.api, self.db, self.admin, message(42, photo="file_1"))
        bot.handle_message(self.api, self.db, self.admin, message(42, photo="file_2"))
        bot.handle_message(self.api, self.db, self.admin, message(42, "/done"))
        save_token = self.admin.states[42].token
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:save:{save_token}"))
        item_id = max(item.id for item in self.db.list_properties(active_only=False))
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:save:{save_token}"))
        self.assertEqual(len(self.db.list_properties(active_only=False)), 3)
        created = self.db.get_property(item_id)
        self.assertEqual(created.title, "Тестовый офис")
        self.assertEqual(created.sale_price, 11_000_000)
        self.assertEqual(created.photos, ("file_1", "file_2"))
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:addphoto:{item_id}"))
        bot.handle_message(self.api, self.db, self.admin, message(42, photo="file_3"))
        bot.handle_message(self.api, self.db, self.admin, message(42, "/done"))
        self.assertEqual(self.db.get_property(item_id).photos, ("file_1", "file_2", "file_3"))
        photo_id = self.db.list_photos(item_id)[0]["id"]
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:photodel:{item_id}:{photo_id}"))
        self.assertEqual(self.db.get_property(item_id).photos, ("file_1", "file_2", "file_3"))
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:confirmphotodel:{item_id}:{photo_id}"))
        self.assertEqual(self.db.get_property(item_id).photos, ("file_2", "file_3"))
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:confirmphotodel:{item_id}:{photo_id}"))
        self.assertEqual(self.db.get_property(item_id).photos, ("file_2", "file_3"))
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:confirmdelete:{item_id}"))
        self.assertIsNone(self.db.get_property(item_id, active_only=False))
        self.db.close()
        self.db = CatalogDB(self.path)
        self.assertEqual(len(self.db.list_properties(active_only=False)), 2)

    def test_draft_preview_shows_card_without_publishing_it(self):
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:add"))
        for answer in (
            "Тестовый офис", "95000", "11000000", "75", "Казань", "Офис",
            "Короткий текст", "Подробное описание", "Парковка",
        ):
            bot.handle_message(self.api, self.db, self.admin, message(42, answer))
        bot.handle_message(self.api, self.db, self.admin, message(42, photo="file_1"))
        token = self.admin.states[42].token
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:previewdraft:{token}"))
        first_preview = next(params for method, params in reversed(self.api.calls) if method == "sendPhoto")
        self.assertEqual(first_preview["photo"], "file_1")
        self.assertIn("Тестовый офис", first_preview["caption"])
        self.assertEqual(len(self.db.list_properties()), 2)
        self.assertEqual(self.admin.states[42].photos, ["file_1"])
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:previewdraft:{token}:sale"))
        sale_preview = next(params for method, params in reversed(self.api.calls) if method == "sendPhoto")
        self.assertIn("Продажа: 11 000 000 ₽", sale_preview["caption"])
        self.assertEqual(len(self.db.list_properties()), 2)

        bot.handle_message(self.api, self.db, self.admin, message(42, photo="file_2"))
        bot.handle_message(self.api, self.db, self.admin, message(42, "/done"))
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:previewdraft:{token}"))
        album = next(params for method, params in reversed(self.api.calls) if method == "sendMediaGroup")
        self.assertEqual([entry["media"] for entry in album["media"]], ["file_1", "file_2"])
        self.assertEqual(self.admin.states[42].mode, "confirm_create")
        self.assertEqual(len(self.db.list_properties()), 2)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM user_events").fetchone()[0], 0)
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:save:{token}"))
        self.assertEqual(len(self.db.list_properties()), 3)

    def test_existing_preview_and_photo_delete_show_selected_photo(self):
        bot.handle_callback(self.api, self.db, self.admin, callback(43, "adm:preview:1"))
        self.assertFalse(any(method in ("sendPhoto", "sendMediaGroup") for method, _ in self.api.calls))

        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:preview:1"))
        album = next(params for method, params in reversed(self.api.calls) if method == "sendMediaGroup")
        self.assertEqual([entry["media"] for entry in album["media"]], ["photo_1", "photo_2"])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM user_events").fetchone()[0], 0)

        photo_id = self.db.list_photos(1)[1]["id"]
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:photodel:1:{photo_id}"))
        selected = next(params for method, params in reversed(self.api.calls) if method == "sendPhoto")
        self.assertEqual(selected["photo"], "photo_2")
        self.assertIn("Фото 2 из 2", selected["caption"])
        self.assertEqual(self.db.get_property(1).photos, ("photo_1", "photo_2"))
        bot.handle_callback(self.api, self.db, self.admin, callback(42, f"adm:confirmphotodel:1:{photo_id}"))
        self.assertEqual(self.db.get_property(1).photos, ("photo_1",))

    def test_deleting_all_cards_does_not_reseed(self):
        for item in self.db.list_properties(active_only=False):
            self.db.delete_property(item.id)
        self.db.close()
        self.db = CatalogDB(self.path)
        self.assertEqual(self.db.list_properties(active_only=False), [])
        self.assertEqual(bot.catalog(self.db)[1]["inline_keyboard"], [[bot.button("← Аренда / продажа", "home")]])

    def test_live_catalog_import_preserves_history_and_is_idempotent(self):
        with self.db.conn:
            self.db.conn.execute("UPDATE properties SET is_demo = 1 WHERE id = 1")
        self.db.upsert_user(user(100))
        self.db.record_event(100, "property_opened", 1)
        self.db.upsert_user(user(101))
        self.db.record_event(101, "property_opened", 2)
        manifest = json.loads((Path(__file__).parents[1] / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(self.db.import_catalog(manifest["batch_id"], manifest["items"]), (True, 1))
        self.assertEqual(len(self.db.list_properties()), 12)
        self.assertIsNone(self.db.get_property(1, active_only=False))
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM property_photos WHERE property_id = 1").fetchone()[0], 0)
        self.assertIsNone(self.db.conn.execute(
            "SELECT last_property_id FROM users WHERE telegram_id = 100").fetchone()[0])
        self.assertEqual(self.db.conn.execute(
            "SELECT last_property_id FROM users WHERE telegram_id = 101").fetchone()[0], 2)
        self.assertIn("71 000 ₽/мес.", bot.catalog(self.db)[1]["inline_keyboard"][0][0]["text"])
        self.assertIn("Аренда: 71 000 ₽/мес. с НДС", bot.detail_caption(self.db.list_properties()[0]))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM user_events").fetchone()[0], 1)
        report_properties, _ = self.db.report_data("0001-01-01 00:00:00", "9999-12-31 23:59:59")
        self.assertEqual(len(report_properties), 12)
        self.assertEqual(self.db.import_catalog(manifest["batch_id"], manifest["items"]), (False, 0))
        self.assertEqual(len(self.db.list_properties()), 12)

    def test_import_cleans_legacy_cards_after_batch_was_loaded(self):
        manifest = json.loads((Path(__file__).parents[1] / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(self.db.import_catalog(manifest["batch_id"], manifest["items"]), (True, 0))
        with self.db.conn:
            self.db.conn.execute("UPDATE properties SET is_demo = 1 WHERE id = 1")
        self.assertEqual(self.db.import_catalog(manifest["batch_id"], manifest["items"]), (False, 1))
        self.assertEqual(len(self.db.list_properties()), 12)

    def test_failed_import_keeps_old_cards_and_events(self):
        with self.db.conn:
            self.db.conn.execute("UPDATE properties SET is_demo = 1 WHERE id = 1")
        self.db.upsert_user(user(100))
        self.db.record_event(100, "property_opened", 1)
        manifest = json.loads((Path(__file__).parents[1] / "catalog.json").read_text(encoding="utf-8"))
        invalid = [dict(manifest["items"][0], price=-1)]
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.import_catalog("invalid", invalid)
        self.assertIsNotNone(self.db.get_property(1, active_only=False))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM user_events").fetchone()[0], 1)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM catalog_imports").fetchone()[0], 0)

    def test_new_database_starts_without_fake_cards(self):
        empty = CatalogDB(Path(self.temp.name) / "empty.sqlite3")
        try:
            self.assertEqual(empty.list_properties(), [])
        finally:
            empty.close()

    def test_local_photos_use_multipart_upload(self):
        class CaptureOpener:
            def __init__(self):
                self.requests = []

            def open(self, req, timeout):
                self.requests.append(req)
                return BytesIO(b'{"ok":true,"result":[{"message_id":1},{"message_id":2}]}')

        manifest = json.loads((Path(__file__).parents[1] / "catalog.json").read_text(encoding="utf-8"))
        first = manifest["items"][0]["photos"][:2]
        api = bot.TelegramAPI("test-token")
        api.opener = CaptureOpener()
        result = api.call("sendMediaGroup", chat_id=100, media=[
            {"type": "photo", "media": first[0], "caption": "Тест"},
            {"type": "photo", "media": first[1]},
        ])
        self.assertEqual(len(result), 2)
        req = api.opener.requests[0]
        self.assertIn("multipart/form-data", req.headers["Content-type"])
        self.assertIn(b'attach://photo0', req.data)
        self.assertIn(b'attach://photo1', req.data)
        self.assertIn(b'\xff\xd8\xff', req.data)

    def test_home_catalog_and_detail_preserve_price_mode(self):
        bot.handle_message(self.api, self.db, self.admin, message(100, "/start"))
        rows = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertEqual([row[0]["text"] for row in rows], ["Объекты. Аренда (не пересылать)", "Объекты. Продажа (не пересылать)"])
        for mode, amount in (("rent", "70 100 ₽/мес."), ("sale", "10 100 000 ₽")):
            bot.handle_callback(self.api, self.db, self.admin, callback(100, f"catalog:{mode}"))
            label = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["text"]
            self.assertEqual(label, f"Москва · 36,3 м² · {amount}")
            bot.handle_callback(self.api, self.db, self.admin, callback(100, f"show:{mode}:1"))
            album = next(params for method, params in reversed(self.api.calls) if method == "sendMediaGroup")
            self.assertIn(amount, album["media"][0]["caption"])
            self.assertEqual(bot.ACTIVE_DETAILS[100].mode, mode)
            controls = next(params for method, params in reversed(self.api.calls)
                            if method == "sendMessage" and "reply_markup" in params)["reply_markup"]["inline_keyboard"]
            self.assertEqual(controls[0][1]["callback_data"], f"view:{mode}:2")
            old_controls = bot.ACTIVE_DETAILS[100].controls_id
            bot.handle_callback(self.api, self.db, self.admin, callback(100, f"view:{mode}:2", old_controls))
            self.assertEqual(bot.ACTIVE_DETAILS[100].mode, mode)
            if mode == "sale":
                selected = next(params for method, params in reversed(self.api.calls) if method == "sendPhoto")
                self.assertIn("Продажа: цена по запросу", selected["caption"])
                self.assertNotIn("90 000", selected["caption"])
            call_count = len(self.api.calls)
            bot.handle_callback(self.api, self.db, self.admin, callback(100, f"view:{mode}:1", old_controls))
            self.assertEqual(len(self.api.calls), call_count + 1)
            self.assertIn("устарела", self.api.calls[-1][1]["text"])
            current_controls = bot.ACTIVE_DETAILS[100].controls_id
            bot.handle_callback(self.api, self.db, self.admin, callback(100, f"back:{mode}:2", current_controls))
            catalog_message = next(params for method, params in reversed(self.api.calls) if method == "sendMessage")
            self.assertIn(amount, catalog_message["reply_markup"]["inline_keyboard"][0][0]["text"])
            self.assertNotIn(100, bot.ACTIVE_DETAILS)
        bot.handle_callback(self.api, self.db, self.admin, callback(100, "home"))
        self.assertEqual(self.api.calls[-1][1]["reply_markup"], bot.home()[1])
        count = len(self.api.calls)
        bot.handle_callback(self.api, self.db, self.admin, callback(100, "show:invalid:1"))
        self.assertEqual(len(self.api.calls), count + 1)

    def test_admin_sale_price_edit_and_preview(self):
        bot.handle_callback(self.api, self.db, self.admin, callback(43, "adm:field:1:sale_price"))
        self.assertNotIn(43, self.admin.states)
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:field:1:sale_price"))
        bot.handle_message(self.api, self.db, self.admin, message(42, "-1"))
        self.assertEqual(self.db.get_property(1).sale_price, 10_100_000)
        bot.handle_message(self.api, self.db, self.admin, message(42, "12 000 000"))
        self.assertEqual(self.db.get_property(1).sale_price, 12_000_000)
        self.assertEqual(self.db.get_property(1).price, 70_100)
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:preview:1:sale"))
        album = next(params for method, params in reversed(self.api.calls) if method == "sendMediaGroup")
        self.assertIn("Продажа: 12 000 000 ₽", album["media"][0]["caption"])
        self.assertNotIn("Аренда:", album["media"][0]["caption"])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM user_events").fetchone()[0], 0)

    def test_catalog_upgrade_updates_existing_card_without_losing_history_or_admin_settings(self):
        batch_id, items = validated_catalog()
        old = dict(items[0], title=items[0]["previous_title"], location=items[0]["previous_location"],
                   price=70100, area=29, sale_price=None, features=["Окупаемость: 10 лет"])
        item_id = self.db.create_property(old, ["media/object_02/01.jpg"])
        self.db.upsert_user(user(100))
        self.db.record_event(100, "property_opened", item_id)
        self.db.mark_report_sent("2026-10-05 07:00:00", "example@example.com")
        self.db.set_active(item_id, False)
        self.db.conn.execute("UPDATE properties SET sort_order = 555 WHERE id = ?", (item_id,))
        self.db.conn.commit()
        self.assertEqual(self.db.import_catalog(batch_id, items), (True, 0))
        card = self.db.get_property(item_id, active_only=False)
        self.assertEqual((card.price, card.sale_price, card.area), (71000, 10100000, 29.4))
        self.assertEqual(card.location, items[0]["location"])
        self.assertEqual(card.features, ())
        self.assertEqual(card.photos, tuple(items[0]["photos"]))
        self.assertFalse(self.db.is_active(item_id))
        self.assertEqual(self.db.conn.execute("SELECT sort_order FROM properties WHERE id = ?", (item_id,)).fetchone()[0], 555)
        self.assertEqual(self.db.conn.execute("SELECT property_id FROM user_events").fetchone()[0], item_id)
        self.assertEqual(self.db.conn.execute("SELECT last_property_id FROM users WHERE telegram_id = 100").fetchone()[0], item_id)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM report_deliveries").fetchone()[0], 1)
        self.assertEqual(len(self.db.list_properties(active_only=False)), 13)
        self.db.update_property(item_id, "description", "Изменено администратором")
        self.assertEqual(self.db.import_catalog(batch_id, items), (False, 0))
        self.assertEqual(self.db.get_property(item_id, active_only=False).description, "Изменено администратором")
        self.db.delete_property(item_id)
        self.assertEqual(self.db.import_catalog(batch_id, items), (False, 0))
        self.assertIsNone(self.db.get_property(item_id, active_only=False))

    def test_catalog_upgrade_matches_legacy_card_without_original_photos(self):
        batch_id, items = validated_catalog()
        old = dict(items[0], title=items[0]["previous_title"], location=items[0]["previous_location"])
        item_id = self.db.create_property(old, ["telegram_file_id"])
        self.db.import_catalog(batch_id, items)
        self.assertEqual(self.db.get_property(item_id).location, items[0]["location"])
        self.assertEqual(len(self.db.list_properties(active_only=False)), 13)

    def test_ambiguous_catalog_upgrade_rolls_back(self):
        batch_id, items = validated_catalog()
        for _ in range(2):
            self.db.create_property(items[0], ["media/object_02/01.jpg"])
        with self.assertRaises(ValueError):
            self.db.import_catalog(batch_id, items)
        self.assertEqual(len(self.db.list_properties()), 4)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM catalog_imports").fetchone()[0], 0)

    def test_new_manifest_is_valid_and_contains_both_prices_without_payback(self):
        _, items = validated_catalog()
        self.assertEqual(len(items), 11)
        self.assertTrue(all(item["sale_price"] > 0 and item["price"] > 0 for item in items))
        self.assertFalse(any("Окупаемость" in json.dumps(item, ensure_ascii=False) for item in items))
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "catalog.json"
            for invalid in (dict(items[0], sale_price=0), dict(items[0], source_row="2")):
                manifest.write_text(json.dumps({"batch_id": "bad", "items": [invalid]}), encoding="utf-8")
                with patch("import_catalog.MANIFEST", manifest), self.assertRaises(ValueError):
                    validated_catalog()

    def test_startup_applies_new_catalog_once_without_network_or_smtp(self):
        class StartupAPI:
            def call(self, method, **params):
                if method == "getMe":
                    return {"username": bot.EXPECTED_BOT_USERNAME}
                if method == "getUpdates":
                    raise KeyboardInterrupt
                raise AssertionError(method)

        path = Path(self.temp.name) / "startup.sqlite3"
        settings = {"BOT_TOKEN": "dummy-token", "DB_PATH": str(path), "ADMIN_IDS": "42"}
        def start():
            output = StringIO()
            with patch("bot.load_setting", side_effect=lambda env, name: settings.get(name, "")), \
                    patch("bot.TelegramAPI", return_value=StartupAPI()), redirect_stdout(output), \
                    self.assertRaises(KeyboardInterrupt):
                bot.main()
            return output.getvalue()

        self.assertIn("Каталог обновлён: 11 объектов", start())
        db = CatalogDB(path)
        try:
            self.assertEqual(len(db.list_properties()), 11)
            item = db.list_properties()[0]
            self.assertEqual(item.sale_price, 10_100_000)
            db.update_property(item.id, "description", "Правка администратора")
        finally:
            db.close()
        self.assertNotIn("Каталог обновлён", start())
        db = CatalogDB(path)
        try:
            self.assertEqual(db.list_properties()[0].description, "Правка администратора")
            self.assertEqual(len(db.list_properties()), 11)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()

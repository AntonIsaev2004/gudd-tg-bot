"""Сквозные проверки каталога, SQLite и админки без запросов к Telegram."""

import tempfile
import unittest
import sqlite3
from pathlib import Path

import bot
from admin import AdminPanel, STEPS
from storage import CatalogDB


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
        self.api = FakeAPI()
        self.admin = AdminPanel(self.api, self.db, {42})
        bot.ACTIVE_DETAILS.clear()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()
        bot.ACTIVE_DETAILS.clear()

    def test_catalog_user_tracking_and_persistence(self):
        self.assertEqual(len(self.db.list_properties()), 8)
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
        self.assertEqual(len(self.db.list_properties()), 8)
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
            self.assertEqual(old_db.conn.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertIn("phone_number", [row["name"] for row in old_db.conn.execute("PRAGMA table_info(user_events)")])
        finally:
            old_db.close()

    def test_admin_access_edit_hide_and_order(self):
        bot.handle_message(self.api, self.db, self.admin, message(43, "/admin"))
        self.assertIn("Нет доступа", self.api.calls[-1][1]["text"])
        bot.handle_callback(self.api, self.db, self.admin, callback(43, "adm:delete:1"))
        bot.handle_callback(self.api, self.db, self.admin, callback(43, "adm:confirmdelete:1"))
        self.assertIsNotNone(self.db.get_property(1))
        bot.handle_message(self.api, self.db, self.admin, message(42, "/admin"))
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:field:1:title"))
        bot.handle_message(self.api, self.db, self.admin, message(42, "Новая квартира"))
        self.assertEqual(self.db.get_property(1).title, "Новая квартира")
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:field:1:area"))
        bot.handle_message(self.api, self.db, self.admin, message(42, "82,5"))
        self.assertEqual(self.db.get_property(1).area, 82.5)
        self.assertIn("82,5 м²", bot.catalog(self.db)[1]["inline_keyboard"][0][0]["text"])
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:demo:1"))
        self.assertFalse(self.db.get_property(1).is_demo)
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:toggle:1"))
        self.assertIsNone(self.db.get_property(1))
        self.assertEqual(len(bot.catalog(self.db)[1]["inline_keyboard"]), 7)
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:toggle:1"))
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:down:1"))
        self.assertEqual([item.id for item in self.db.list_properties()][:2], [2, 1])

    def test_admin_creates_and_deletes_card_with_photos(self):
        bot.handle_callback(self.api, self.db, self.admin, callback(42, "adm:add"))
        answers = (
            "Тестовый дом", "9500000", "75", "Казань", "2 комнаты",
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
        self.assertEqual(len(self.db.list_properties(active_only=False)), 9)
        created = self.db.get_property(item_id)
        self.assertEqual(created.title, "Тестовый дом")
        self.assertEqual(created.photos, ("file_1", "file_2"))
        self.assertFalse(created.is_demo)
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
        self.assertEqual(len(self.db.list_properties(active_only=False)), 8)

    def test_deleting_all_cards_does_not_reseed_demo(self):
        for item in self.db.list_properties(active_only=False):
            self.db.delete_property(item.id)
        self.db.close()
        self.db = CatalogDB(self.path)
        self.assertEqual(self.db.list_properties(active_only=False), [])
        self.assertIsNone(bot.catalog(self.db)[1])


if __name__ == "__main__":
    unittest.main()

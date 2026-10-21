"""Общая шапка: права, сохранение эмодзи, предпросмотр и работа без Premium."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import bot
from admin import AdminPanel
from card_header import CardHeader, DEFAULT_HEADER_TEXT, LEGACY_HEADER_TEXT, call_with_emoji_fallback, initialize_card_header
from import_catalog import validated_catalog
from storage import CatalogDB
from test_workflows import FakeAPI, callback, message


def header_message(text="🏢 Контакт: +7 987 404 0101"):
    result = message(42, text)
    result["entities"] = [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "123456"}]
    return result


class CardHeaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "header.sqlite3"
        self.db = CatalogDB(self.path)
        _, self.items = validated_catalog()
        self.first = self.db.create_property(self.items[0], ["photo1", "photo2"])
        self.second = self.db.create_property(self.items[1], ["photo3"])
        self.api = FakeAPI()
        self.admin = AdminPanel(self.api, self.db, {42}, bot.send_detail)
        bot.ACTIVE_DETAILS.clear()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()
        bot.ACTIVE_DETAILS.clear()

    def test_default_logo_is_checked_on_startup_without_overwriting_admin_changes(self):
        api = Mock()
        api.call.return_value = {"sticker_type": "custom_emoji", "stickers": [{"custom_emoji_id": "123456"}]}
        self.assertEqual(self.db.get_card_header().text, DEFAULT_HEADER_TEXT)
        initialize_card_header(api, self.db)
        self.assertIn('<tg-emoji emoji-id="123456">🏢</tg-emoji>', self.db.get_card_header().as_html())
        initialize_card_header(api, self.db)
        self.assertEqual(api.call.call_count, 2)
        self.db.set_card_header(CardHeader("Свой текст администратора"))
        self.db.close()
        self.db = CatalogDB(self.path)
        initialize_card_header(api, self.db)
        self.assertEqual(api.call.call_count, 2)
        self.assertEqual(self.db.get_card_header().text, "Свой текст администратора")

    def test_deleted_logo_is_refreshed_and_saved_for_all_cards(self):
        self.db.set_card_header(CardHeader.from_message(header_message(DEFAULT_HEADER_TEXT)))
        self.db.close()
        self.db = CatalogDB(self.path)
        api = Mock()
        api.call.return_value = {"sticker_type": "custom_emoji", "stickers": [{"custom_emoji_id": "654321"}]}
        initialize_card_header(api, self.db)
        updated = self.db.get_card_header()
        self.assertEqual(updated.text, DEFAULT_HEADER_TEXT)
        self.assertEqual(updated.emoji_entities[0]["custom_emoji_id"], "654321")
        for item in self.db.list_properties(active_only=False):
            for mode in ("rent", "sale"):
                caption = bot.detail_caption(item, mode=mode, header=updated)
                self.assertTrue(caption.endswith(updated.as_html()))
                self.assertIn('emoji-id="654321"', caption)
                self.assertNotIn('emoji-id="123456"', caption)
        self.db.close()
        self.db = CatalogDB(self.path)
        self.assertEqual(self.db.get_card_header(), updated)

    def test_logo_refresh_keeps_existing_choice_and_survives_failed_lookup(self):
        header = CardHeader.from_message(header_message(DEFAULT_HEADER_TEXT))
        self.db.set_card_header(header)
        api = Mock()
        api.call.return_value = {"sticker_type": "custom_emoji", "stickers": [
            {"custom_emoji_id": "654321"}, {"custom_emoji_id": "123456"}]}
        initialize_card_header(api, self.db)
        self.assertEqual(self.db.get_card_header(), header)
        api.call.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            initialize_card_header(api, self.db)
        self.assertEqual(self.db.get_card_header(), header)
        api.call.side_effect = None
        for pack in ({"sticker_type": "custom_emoji", "stickers": []},
                     {"sticker_type": "regular", "stickers": [{"custom_emoji_id": "654321"}]}):
            api.call.return_value = pack
            with self.assertRaises(ValueError):
                initialize_card_header(api, self.db)
            self.assertEqual(self.db.get_card_header(), header)
        self.db.set_card_header(CardHeader(DEFAULT_HEADER_TEXT))
        api.reset_mock()
        initialize_card_header(api, self.db)
        api.call.assert_not_called()

    def test_failed_logo_lookup_leaves_default_text_and_can_retry(self):
        api = Mock()
        api.call.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            initialize_card_header(api, self.db)
        self.assertFalse(self.db.has_card_header())
        self.assertIn("+7 987 404 0101", self.db.get_card_header().text)
        api.call.side_effect = None
        api.call.return_value = {"sticker_type": "custom_emoji", "stickers": [{"custom_emoji_id": "123456"}]}
        initialize_card_header(api, self.db)
        self.assertTrue(self.db.has_card_header())

    def test_old_default_contact_migrates_once_and_preserves_logo(self):
        legacy = CardHeader.from_message(header_message(LEGACY_HEADER_TEXT))
        self.db.set_card_header(legacy)
        self.db.close()
        self.db = CatalogDB(self.path)
        updated = self.db.get_card_header()
        self.assertEqual(updated.text, DEFAULT_HEADER_TEXT)
        self.assertEqual(updated.emoji_entities, legacy.emoji_entities)
        # После миграции даже явно возвращённый администратором старый текст не заменяется.
        self.db.set_card_header(legacy)
        self.db.close()
        self.db = CatalogDB(self.path)
        self.assertEqual(self.db.get_card_header(), legacy)

    def test_contact_migration_preserves_custom_text_and_disabled_block(self):
        for index, header in enumerate((CardHeader("Другой телефон: +7 000 000 0000"), CardHeader(""))):
            with self.subTest(header=header.text):
                path = Path(self.temp.name) / f"custom-{index}.sqlite3"
                with_db = CatalogDB(path)
                with_db.set_card_header(header)
                with_db.close()
                with_db = CatalogDB(path)
                self.assertEqual(with_db.get_card_header(), header)
                with_db.close()

    def test_header_edit_is_admin_only_and_applies_to_all_cards_and_previews(self):
        before = self.db.list_properties(active_only=False)
        self.admin.handle_callback(callback(43, "adm:editheader"))
        self.assertNotIn(43, self.admin.states)
        self.assertFalse(self.db.has_card_header())
        self.admin.handle_callback(callback(42, "adm:editheader"))
        self.admin.handle_message(header_message())
        self.assertNotIn(42, self.admin.states)
        self.assertEqual(before, self.db.list_properties(active_only=False))
        expected = '<tg-emoji emoji-id="123456">🏢</tg-emoji> Контакт: +7 987 404 0101'
        for mode in ("rent", "sale"):
            bot.handle_callback(self.api, self.db, self.admin, callback(100, f"show:{mode}:{self.first}"))
            album = next(params for method, params in reversed(self.api.calls) if method == "sendMediaGroup")
            self.assertTrue(album["media"][0]["caption"].endswith("\n\n" + expected))
            self.assertNotIn("caption", album["media"][1])
        self.db.set_active(self.second, False)
        self.admin.handle_callback(callback(42, f"adm:preview:{self.second}:sale"))
        preview = next(params for method, params in reversed(self.api.calls) if method == "sendPhoto")
        self.assertTrue(preview["caption"].endswith(expected))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM user_events WHERE event_type = 'property_opened'").fetchone()[0], 2)
        self.db.close()
        self.db = CatalogDB(self.path)
        new_id = self.db.create_property(self.items[2], ["new_photo"])
        caption = bot.detail_caption(self.db.get_property(new_id), header=self.db.get_card_header())
        self.assertTrue(caption.endswith(expected))

    def test_contact_stays_last_when_photos_are_unavailable(self):
        header = CardHeader(DEFAULT_HEADER_TEXT)
        item = self.db.get_property(self.first)
        for mode in ("rent", "sale"):
            with self.subTest(mode=mode):
                api = FakeAPI()
                original_call = api.call

                def call(method, **params):
                    if method in ("sendMediaGroup", "sendPhoto"):
                        raise bot.BotApiError(400, "PHOTO_UNAVAILABLE")
                    return original_call(method, **params)

                api.call = call
                bot.send_detail(api, 100, item, mode=mode, header=header)
                text = api.calls[-1][1]["text"]
                self.assertIn("Фото временно недоступны", text)
                self.assertTrue(text.endswith("\n\n" + header.as_html()))
                self.assertEqual(text.count(header.as_html()), 1)

    def test_header_cancel_navigation_skip_and_invalid_input(self):
        initial = self.db.get_card_header()
        self.admin.handle_callback(callback(42, "adm:editheader"))
        self.admin.handle_message(message(42, photo="photo"))
        self.assertIn(42, self.admin.states)
        self.assertEqual(initial, self.db.get_card_header())
        self.admin.handle_message(message(42, "/cancel"))
        self.assertNotIn(42, self.admin.states)
        self.admin.handle_callback(callback(42, "adm:editheader"))
        self.admin.handle_callback(callback(42, "adm:menu"))
        self.assertNotIn(42, self.admin.states)
        self.admin.handle_callback(callback(42, "adm:editheader"))
        self.admin.handle_message(message(42, "/skip"))
        self.assertEqual(self.db.get_card_header().text, "")
        initialize_card_header(Mock(), self.db)
        self.assertEqual(self.db.get_card_header().text, "")

    def test_utf16_offsets_and_html_are_preserved_safely(self):
        header = CardHeader.from_message({"text": "  📞 🏢 <номер> & телефон  ", "entities": [
            {"type": "custom_emoji", "offset": 5, "length": 2, "custom_emoji_id": "987654"},
        ]})
        self.assertEqual(header.text, "📞 🏢 <номер> & телефон")
        self.assertEqual(header.as_html(), '📞 <tg-emoji emoji-id="987654">🏢</tg-emoji> &lt;номер&gt; &amp; телефон')
        self.assertEqual(CardHeader.from_json(header.to_json()), header)
        for entity in ({"offset": 1, "length": 1, "custom_emoji_id": "123"},
                       {"offset": 0, "length": 2, "custom_emoji_id": '123"><b>'},
                       {"offset": 100, "length": 2, "custom_emoji_id": "123"}):
            with self.assertRaises(ValueError):
                CardHeader.from_message({"text": "🏢 Текст", "entities": [dict(entity, type="custom_emoji")]})
        with self.assertRaises(ValueError):
            CardHeader.from_message({"text": "x" * 241})

    def test_expired_premium_retries_full_album_with_plain_emoji(self):
        class NonPremiumAPI(FakeAPI):
            def call(self, method, **params):
                if method == "sendMediaGroup" and "<tg-emoji" in params["media"][0].get("caption", ""):
                    self.calls.append((method, params))
                    raise bot.BotApiError(400, "CUSTOM_EMOJI_NOT_ALLOWED")
                return super().call(method, **params)
        api = NonPremiumAPI()
        header = CardHeader.from_message(header_message())
        sent = bot.send_detail(api, 100, self.db.get_property(self.first), header=header)
        self.assertEqual(len(sent), 2)
        calls = [params for method, params in api.calls if method == "sendMediaGroup"]
        self.assertEqual(len(calls), 2)
        self.assertEqual([entry["media"] for entry in calls[-1]["media"]], ["photo1", "photo2"])
        self.assertNotIn("<tg-emoji", calls[-1]["media"][0]["caption"])
        self.assertIn("+7 987 404 0101", calls[-1]["media"][0]["caption"])
        self.assertFalse(any(method == "sendPhoto" for method, _ in api.calls))
        api = Mock()
        api.call.side_effect = OSError("network disconnected")
        with self.assertRaises(OSError):
            call_with_emoji_fallback(api, "sendMessage", text=header.as_html())
        self.assertEqual(api.call.call_count, 1)

    def test_long_card_keeps_complete_text_in_one_description_after_photos(self):
        self.db.update_property(self.first, "description", "Подробное описание. " * 40)
        header = CardHeader("Шапка " + "контакт " * 25)
        item = self.db.get_property(self.first)
        expected = bot.detail_caption(item, header=header)
        sent = bot.send_detail(self.api, 100, item, header=header)
        album = next(params for method, params in self.api.calls if method == "sendMediaGroup")
        self.assertTrue(all("caption" not in entry for entry in album["media"]))
        descriptions = [params for method, params in self.api.calls if method == "sendMessage"]
        self.assertEqual(len(descriptions), 1)
        self.assertEqual(descriptions[0]["text"], expected)
        self.assertEqual(len(sent), 3)


if __name__ == "__main__":
    unittest.main()

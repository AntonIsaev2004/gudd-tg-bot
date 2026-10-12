"""Команда очистки во всех полях админки без вызовов Telegram."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import bot
from admin import AdminPanel, AdminState, STEPS, parse_value
from import_catalog import validated_catalog
from storage import CatalogDB
from test_workflows import FakeAPI, message


class SkipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "skip.sqlite3"
        self.db = CatalogDB(self.path)
        self.api = FakeAPI()
        self.admin = AdminPanel(self.api, self.db, {42}, bot.send_detail)
        _, self.items = validated_catalog()
        self.item_id = self.db.create_property(self.items[0], ["photo"])

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_every_field_handles_plain_and_addressed_skip(self):
        optional = {"title": "", "teaser": "", "features": ()}
        for field, _ in STEPS:
            for command in ("/skip", "/skip@Gudd_centr_nedvizhimosti_bot", " /skip@Gudd_centr_nedvizhimosti_bot "):
                with self.subTest(field=field, command=command):
                    if field in optional:
                        self.assertEqual(parse_value(field, command), optional[field])
                    else:
                        with self.assertRaisesRegex(ValueError, "поле обязательно"):
                            parse_value(field, command)
        self.assertEqual(parse_value("description", "В инструкции есть команда /skip."), "В инструкции есть команда /skip.")

    def test_create_and_edit_keep_waiting_for_required_values(self):
        optional = {"title", "teaser", "features"}
        for index, (field, _) in enumerate(STEPS):
            for command in ("/skip", "/skip@Gudd_centr_nedvizhimosti_bot"):
                with self.subTest(field=field, command=command):
                    state = AdminState("create", step=index)
                    self.admin.states[42] = state
                    self.admin.handle_message(message(42, command))
                    self.assertEqual(state.step, index + 1 if field in optional else index)
                    self.assertEqual(field in state.values, field in optional)
                    before = self.db.get_property(self.item_id)
                    state = AdminState("edit_field", item_id=self.item_id, field_name=field)
                    self.admin.states[42] = state
                    self.admin.handle_message(message(42, command))
                    after = self.db.get_property(self.item_id)
                    if field in optional:
                        self.assertNotIn(42, self.admin.states)
                        self.assertEqual(getattr(after, field), () if field == "features" else "")
                    else:
                        self.assertIs(self.admin.states[42], state)
                        self.assertEqual(before, after)
                        self.assertIn("поле обязательно", self.api.calls[-1][1]["text"])

    def test_existing_skip_markers_are_removed_without_losing_real_text_or_other_data(self):
        original = dict(self.items[0], title="/skip@Gudd_centr_nedvizhimosti_bot", teaser=" /skip ",
                        features=["Парковка", "/skip", "/skip@Gudd_centr_nedvizhimosti_bot", "Текст /skip внутри"])
        item_id = self.db.create_property(original, ["photo_1", "photo_2"])
        self.db.set_active(item_id, False)
        self.db.upsert_user({"id": 100, "first_name": "Посетитель"})
        self.db.record_event(100, "property_opened", item_id)
        before = self.db.get_property(item_id, active_only=False)
        self.db.close()
        self.db = CatalogDB(self.path)
        after = self.db.get_property(item_id, active_only=False)
        self.assertEqual(after, replace(before, title="", teaser="", features=("Парковка", "Текст /skip внутри")))
        self.assertFalse(self.db.is_active(item_id))
        self.assertEqual(self.db.conn.execute("SELECT property_id FROM user_events").fetchone()[0], item_id)
        self.db.close()
        self.db = CatalogDB(self.path)
        self.assertEqual(after, self.db.get_property(item_id, active_only=False))


if __name__ == "__main__":
    unittest.main()

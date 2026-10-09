"""SQLite-хранилище каталога и истории действий пользователей."""

import json
import sqlite3
import re
from pathlib import Path

from models import Property
from catalog_labels import initial_button_text


class CatalogDB:
    def __init__(self, path):
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        try:
            self._initialize()
        except Exception:
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    def _initialize(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS properties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                price INTEGER NOT NULL CHECK (price > 0),
                area REAL NOT NULL CHECK (area > 0),
                location TEXT NOT NULL,
                rooms TEXT NOT NULL,
                teaser TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL,
                features_json TEXT NOT NULL DEFAULT '[]',
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS property_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
                media TEXT NOT NULL,
                position INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_photos_property ON property_photos(property_id, position);
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT,
                is_premium INTEGER,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_property_id INTEGER,
                phone_number TEXT,
                phone_shared_at TEXT
            );
            CREATE TABLE IF NOT EXISTS user_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                property_id INTEGER,
                phone_number TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_events_user_time ON user_events(telegram_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_property ON user_events(property_id);
            CREATE INDEX IF NOT EXISTS idx_events_type_time ON user_events(event_type, created_at);
            CREATE TABLE IF NOT EXISTS report_deliveries (
                period_end TEXT PRIMARY KEY,
                recipient TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS catalog_imports (
                batch_id TEXT PRIMARY KEY,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with self.conn:
                self.conn.execute("PRAGMA user_version = 1")
        if version < 2:
            with self.conn:
                columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(users)")}
                if "phone_number" not in columns:
                    self.conn.execute("ALTER TABLE users ADD COLUMN phone_number TEXT")
                if "phone_shared_at" not in columns:
                    self.conn.execute("ALTER TABLE users ADD COLUMN phone_shared_at TEXT")
                self.conn.execute("PRAGMA user_version = 2")
        if version < 3:
            with self.conn:
                columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(user_events)")}
                if "phone_number" not in columns:
                    self.conn.execute("ALTER TABLE user_events ADD COLUMN phone_number TEXT")
                self.conn.execute(
                    """UPDATE user_events SET phone_number = (
                           SELECT phone_number FROM users WHERE users.telegram_id = user_events.telegram_id
                       )
                       WHERE event_type = 'phone_shared' AND phone_number IS NULL"""
                )
                self.conn.execute("PRAGMA user_version = 3")
        if version < 4:
            with self.conn:
                self.conn.execute("PRAGMA user_version = 4")
        if version < 5:
            with self.conn:
                rows = self.conn.execute("SELECT id, features_json FROM properties").fetchall()
                for row in rows:
                    features = json.loads(row["features_json"])
                    cleaned = [feature for feature in features if feature != "Часть изображений — визуализации"]
                    if cleaned != features:
                        self.conn.execute(
                            "UPDATE properties SET features_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (json.dumps(cleaned, ensure_ascii=False), row["id"]),
                        )
                self.conn.execute("PRAGMA user_version = 5")
        if version < 6:
            with self.conn:
                columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(properties)")}
                if "sale_price" not in columns:
                    self.conn.execute("ALTER TABLE properties ADD COLUMN sale_price INTEGER CHECK (sale_price IS NULL OR sale_price > 0)")
                if "source_row" not in columns:
                    self.conn.execute("ALTER TABLE properties ADD COLUMN source_row INTEGER")
                self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_property_source_row ON properties(source_row) WHERE source_row IS NOT NULL")
                rows = self.conn.execute("SELECT id, features_json FROM properties").fetchall()
                for row in rows:
                    features = json.loads(row["features_json"])
                    cleaned = [feature for feature in features if not re.fullmatch(
                        r"Окупаемость\s*:?\s*10\s*лет", feature, re.IGNORECASE
                    )]
                    if cleaned != features:
                        self.conn.execute("UPDATE properties SET features_json = ? WHERE id = ?",
                                          (json.dumps(cleaned, ensure_ascii=False), row["id"]))
                self.conn.execute("PRAGMA user_version = 6")
        if version < 7:
            self._migrate_text_prices()
        if version < 8:
            self._migrate_button_texts()

    def _migrate_button_texts(self):
        """Сохраняет прежние подписи как отдельные редактируемые тексты."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(properties)")}
            for column in ("rent_button_text", "sale_button_text"):
                if column not in columns:
                    self.conn.execute(f"ALTER TABLE properties ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            rows = self.conn.execute("SELECT id, location, area, price, sale_price, rent_button_text, sale_button_text FROM properties").fetchall()
            for row in rows:
                self.conn.execute(
                    "UPDATE properties SET rent_button_text = ?, sale_button_text = ? WHERE id = ?",
                    (row["rent_button_text"] or initial_button_text(row["location"], row["area"], row["price"]),
                     row["sale_button_text"] or initial_button_text(row["location"], row["area"], row["sale_price"], mode="sale"),
                     row["id"]),
                )
            self.conn.execute("PRAGMA user_version = 8")

    def _migrate_text_prices(self):
        """Меняет тип цен на TEXT одной транзакцией, сохраняя ID, связи и счётчик ID."""
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.conn:
                # BEGIN нужен до CREATE TABLE: все изменения схемы тоже должны откатываться.
                self.conn.execute("BEGIN IMMEDIATE")
                sequence = self.conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'properties'").fetchone()
                old_sequence = sequence[0] if sequence else 0
                schema_objects = self.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name = 'properties' AND type IN ('index', 'trigger') AND sql IS NOT NULL"
                ).fetchall()
                self.conn.execute("""
                    CREATE TABLE properties_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        price TEXT NOT NULL CHECK (length(trim(price)) BETWEEN 1 AND 50),
                        area REAL NOT NULL CHECK (area > 0),
                        location TEXT NOT NULL,
                        rooms TEXT NOT NULL,
                        teaser TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL,
                        features_json TEXT NOT NULL DEFAULT '[]',
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                        is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        sale_price TEXT CHECK (sale_price IS NULL OR length(trim(sale_price)) BETWEEN 1 AND 50),
                        source_row INTEGER
                    )
                """)
                self.conn.execute("""
                    INSERT INTO properties_new
                        (id, title, price, area, location, rooms, teaser, description, features_json,
                         sort_order, is_active, is_demo, created_at, updated_at, sale_price, source_row)
                    SELECT id, title, CAST(price AS TEXT), area, location, rooms, teaser, description, features_json,
                           sort_order, is_active, is_demo, created_at, updated_at, CAST(sale_price AS TEXT), source_row
                    FROM properties
                """)
                self.conn.execute("DROP TABLE properties")
                self.conn.execute("ALTER TABLE properties_new RENAME TO properties")
                for schema_object in schema_objects:
                    self.conn.execute(schema_object["sql"])
                cursor = self.conn.execute(
                    "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'properties'", (old_sequence,)
                )
                if not cursor.rowcount:
                    self.conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('properties', ?)", (old_sequence,))
                if self.conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise sqlite3.IntegrityError("Не удалось сохранить связи при обновлении полей цены")
                self.conn.execute("PRAGMA user_version = 7")
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _property_from_row(self, row):
        photos = self.conn.execute(
            "SELECT media FROM property_photos WHERE property_id = ? ORDER BY position, id", (row["id"],)
        ).fetchall()
        return Property(
            row["id"], row["title"], row["price"], row["area"], row["location"],
            row["rooms"], row["teaser"], row["description"],
            tuple(json.loads(row["features_json"])), tuple(photo["media"] for photo in photos),
            sale_price=row["sale_price"],
            rent_button_text=row["rent_button_text"], sale_button_text=row["sale_button_text"],
        )

    def list_properties(self, active_only=True):
        where = "WHERE is_active = 1" if active_only else ""
        rows = self.conn.execute(
            f"SELECT * FROM properties {where} ORDER BY sort_order, id"
        ).fetchall()
        return [self._property_from_row(row) for row in rows]

    def get_property(self, item_id, active_only=True):
        row = self.conn.execute(
            "SELECT * FROM properties WHERE id = ? AND (? = 0 OR is_active = 1)",
            (item_id, int(active_only)),
        ).fetchone()
        return self._property_from_row(row) if row else None

    def is_active(self, item_id):
        row = self.conn.execute("SELECT is_active FROM properties WHERE id = ?", (item_id,)).fetchone()
        return bool(row[0]) if row else False

    def create_property(self, values, photos):
        with self.conn:
            order = self.conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM properties").fetchone()[0]
            cursor = self.conn.execute(
                """INSERT INTO properties
                   (title, price, area, location, rooms, teaser, description, features_json, sort_order, sale_price,
                    rent_button_text, sale_button_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (values["title"], values["price"], values["area"], values["location"],
                 values["rooms"], values["teaser"], values["description"],
                 json.dumps(values["features"], ensure_ascii=False), order, values.get("sale_price"),
                 values.get("rent_button_text") or initial_button_text(values.get("button_address", values["location"]), values["area"], values["price"]),
                 values.get("sale_button_text") or initial_button_text(values.get("button_address", values["location"]), values["area"], values.get("sale_price"), mode="sale")),
            )
            item_id = cursor.lastrowid
            self.conn.executemany(
                "INSERT INTO property_photos(property_id, media, position) VALUES (?, ?, ?)",
                [(item_id, media, index) for index, media in enumerate(photos)],
            )
        return item_id

    def import_catalog(self, batch_id, items):
        """Применяет партию один раз, обновляя исходные карточки с сохранением ID и истории."""
        with self.conn:
            imported = self.conn.execute(
                "SELECT 1 FROM catalog_imports WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if not imported:
                order = self.conn.execute("SELECT COALESCE(MIN(sort_order), 0) - ? FROM properties", (len(items),)).fetchone()[0]
                for item in items:
                    source_row = item.get("source_row")
                    matched = self.conn.execute(
                        "SELECT id FROM properties WHERE source_row = ?", (source_row,)
                    ).fetchone() if source_row is not None else None
                    if matched is None and source_row is not None:
                        candidates = self.conn.execute(
                            """SELECT DISTINCT p.id FROM properties p JOIN property_photos f ON f.property_id = p.id
                               WHERE p.is_demo = 0 AND p.source_row IS NULL AND f.media LIKE ?""",
                            (f"media/object_{source_row:02d}/%",),
                        ).fetchall()
                        if not candidates and item.get("previous_title") and item.get("previous_location"):
                            candidates = self.conn.execute(
                                """SELECT id FROM properties WHERE is_demo = 0 AND source_row IS NULL
                                   AND (title = ? OR location = ?)""",
                                (item["previous_title"], item["previous_location"]),
                            ).fetchall()
                        if len(candidates) > 1:
                            raise ValueError(f"Несколько карточек соответствуют строке {source_row}; обновление отменено.")
                        matched = candidates[0] if candidates else None
                    values = (item["title"], item["price"], item["area"], item["location"],
                              item["rooms"], item["teaser"], item["description"],
                              json.dumps(item["features"], ensure_ascii=False), item.get("sale_price"), source_row,
                              item.get("rent_button_text") or initial_button_text(item.get("button_address", item["location"]), item["area"], item["price"]),
                              item.get("sale_button_text") or initial_button_text(item.get("button_address", item["location"]), item["area"], item.get("sale_price"), mode="sale"))
                    if matched:
                        item_id = matched["id"]
                        self.conn.execute(
                            """UPDATE properties SET title = ?, price = ?, area = ?, location = ?, rooms = ?,
                               teaser = ?, description = ?, features_json = ?, sale_price = ?, source_row = ?,
                               rent_button_text = ?, sale_button_text = ?,
                               updated_at = CURRENT_TIMESTAMP WHERE id = ?""", values + (item_id,),
                        )
                        self.conn.execute("DELETE FROM property_photos WHERE property_id = ?", (item_id,))
                    else:
                        cursor = self.conn.execute(
                            """INSERT INTO properties
                               (title, price, area, location, rooms, teaser, description,
                                features_json, sale_price, source_row, rent_button_text, sale_button_text, sort_order, is_active, is_demo)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)""", values + (order,),
                        )
                        item_id = cursor.lastrowid
                    self.conn.executemany(
                        "INSERT INTO property_photos(property_id, media, position) VALUES (?, ?, ?)",
                        [(item_id, media, index) for index, media in enumerate(item["photos"])],
                    )
                    order += 1
                self.conn.execute("INSERT INTO catalog_imports(batch_id) VALUES (?)", (batch_id,))
            demo_count = self.conn.execute("SELECT COUNT(*) FROM properties WHERE is_demo = 1").fetchone()[0]
            if demo_count:
                self.conn.execute(
                    "DELETE FROM user_events WHERE property_id IN (SELECT id FROM properties WHERE is_demo = 1)"
                )
                self.conn.execute(
                    """UPDATE users SET last_property_id = NULL
                       WHERE last_property_id IN (SELECT id FROM properties WHERE is_demo = 1)"""
                )
                self.conn.execute("DELETE FROM properties WHERE is_demo = 1")
        return not bool(imported), demo_count

    def apply_title_corrections(self, batch_id, items):
        """Один раз заменяет исходные адресные заголовки, сохраняя правки администраторов."""
        corrections = [item for item in items if item.get("title_before_address_cleanup")]
        if not corrections:
            return 0
        correction_id = f"{batch_id}:titles-without-address-v2"
        with self.conn:
            if self.conn.execute("SELECT 1 FROM catalog_imports WHERE batch_id = ?", (correction_id,)).fetchone():
                return 0
            count = 0
            for item in corrections:
                cursor = self.conn.execute(
                    """UPDATE properties SET title = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE source_row = ? AND title != ? AND title IN (?, ?, ?)""",
                    (item["title"], item["source_row"], item["title"], item["title_before_address_cleanup"],
                     item["location"], item.get("button_address", item["location"])),
                )
                count += cursor.rowcount
            self.conn.execute("INSERT INTO catalog_imports(batch_id) VALUES (?)", (correction_id,))
        return count

    def apply_button_text_defaults(self, batch_id, items):
        """Один раз сокращает исходные подписи, не меняя цены или собственный текст админа."""
        correction_id = f"{batch_id}:independent-button-texts-v1"
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if self.conn.execute("SELECT 1 FROM catalog_imports WHERE batch_id = ?", (correction_id,)).fetchone():
                return 0
            count = 0
            for item in items:
                address = item.get("button_address")
                if not address:
                    continue
                row = self.conn.execute("SELECT * FROM properties WHERE source_row = ?", (item["source_row"],)).fetchone()
                if row is None:
                    continue
                for column, mode, amount in (("rent_button_text", "rent", row["price"]),
                                              ("sale_button_text", "sale", row["sale_price"])):
                    previous = initial_button_text(row["location"], row["area"], amount, mode=mode)
                    label = initial_button_text(address, row["area"], amount, mode=mode)
                    if row[column] in ("", previous) and row[column] != label:
                        self.conn.execute(f"UPDATE properties SET {column} = ? WHERE id = ?", (label, row["id"]))
                        count += 1
            self.conn.execute("INSERT INTO catalog_imports(batch_id) VALUES (?)", (correction_id,))
        return count

    def update_property(self, item_id, field, value):
        columns = {
            "title": "title", "price": "price", "area": "area", "location": "location",
            "rooms": "rooms", "teaser": "teaser", "description": "description",
            "features": "features_json",
            "sale_price": "sale_price",
            "rent_button_text": "rent_button_text", "sale_button_text": "sale_button_text",
        }
        if field not in columns:
            raise ValueError("Недопустимое поле объекта")
        if field == "features":
            value = json.dumps(value, ensure_ascii=False)
        with self.conn:
            cursor = self.conn.execute(
                f"UPDATE properties SET {columns[field]} = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (value, item_id),
            )
        return cursor.rowcount > 0

    def set_active(self, item_id, active):
        with self.conn:
            self.conn.execute(
                "UPDATE properties SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(active), item_id),
            )

    def delete_property(self, item_id):
        with self.conn:
            self.conn.execute("DELETE FROM properties WHERE id = ?", (item_id,))

    def move_property(self, item_id, direction):
        items = self.list_properties(active_only=False)
        ids = [item.id for item in items]
        if item_id not in ids:
            return False
        index = ids.index(item_id)
        other_index = index + direction
        if other_index < 0 or other_index >= len(ids):
            return False
        first_order = self.conn.execute("SELECT sort_order FROM properties WHERE id = ?", (item_id,)).fetchone()[0]
        other_id = ids[other_index]
        other_order = self.conn.execute("SELECT sort_order FROM properties WHERE id = ?", (other_id,)).fetchone()[0]
        with self.conn:
            self.conn.execute("UPDATE properties SET sort_order = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (other_order, item_id))
            self.conn.execute("UPDATE properties SET sort_order = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (first_order, other_id))
        return True

    def add_photo(self, item_id, media):
        with self.conn:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM property_photos WHERE property_id = ?", (item_id,)
            ).fetchone()[0]
            if count >= 10:
                return False
            self.conn.execute(
                "INSERT INTO property_photos(property_id, media, position) VALUES (?, ?, ?)",
                (item_id, media, count),
            )
        return True

    def list_photos(self, item_id):
        return self.conn.execute(
            "SELECT id, media FROM property_photos WHERE property_id = ? ORDER BY position, id", (item_id,)
        ).fetchall()

    def remove_photo(self, item_id, photo_id):
        rows = self.conn.execute(
            "SELECT id FROM property_photos WHERE property_id = ? ORDER BY position, id", (item_id,)
        ).fetchall()
        if photo_id not in [row["id"] for row in rows]:
            return False
        with self.conn:
            self.conn.execute("DELETE FROM property_photos WHERE id = ? AND property_id = ?", (photo_id, item_id))
            for position, row in enumerate(row for row in rows if row["id"] != photo_id):
                self.conn.execute("UPDATE property_photos SET position = ? WHERE id = ?", (position, row["id"]))
        return True

    def upsert_user(self, telegram_user):
        if not telegram_user or not isinstance(telegram_user.get("id"), int):
            return None
        user_id = telegram_user["id"]
        with self.conn:
            self.conn.execute(
                """INSERT INTO users
                   (telegram_id, username, first_name, last_name, language_code, is_premium)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                     username = excluded.username, first_name = excluded.first_name,
                     last_name = excluded.last_name, language_code = excluded.language_code,
                     is_premium = excluded.is_premium, last_seen_at = CURRENT_TIMESTAMP""",
                (user_id, telegram_user.get("username"), telegram_user.get("first_name"),
                 telegram_user.get("last_name"), telegram_user.get("language_code"),
                 int(telegram_user["is_premium"]) if "is_premium" in telegram_user else None),
            )
        return user_id

    def record_event(self, user_id, event_type, property_id=None):
        if user_id is None:
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO user_events(telegram_id, event_type, property_id) VALUES (?, ?, ?)",
                (user_id, event_type, property_id),
            )
            if event_type == "property_opened":
                self.conn.execute(
                    "UPDATE users SET last_property_id = ? WHERE telegram_id = ?", (property_id, user_id)
                )

    def report_data(self, start_utc, end_utc):
        visits = self.conn.execute(
            """SELECT e.telegram_id, e.property_id, e.created_at,
                      u.username, u.first_name, u.last_name
               FROM user_events AS e JOIN users AS u ON u.telegram_id = e.telegram_id
               WHERE e.event_type = 'property_opened'
                 AND e.created_at >= ? AND e.created_at < ?
               ORDER BY e.created_at, e.id""",
            (start_utc, end_utc),
        ).fetchall()
        visited_ids = {row["property_id"] for row in visits}
        properties = [
            row for row in self.conn.execute(
                "SELECT id, title, is_active FROM properties ORDER BY sort_order, id"
            ).fetchall()
            if row["is_active"] or row["id"] in visited_ids
        ]
        return properties, visits

    def last_report_period_end(self):
        row = self.conn.execute("SELECT MAX(period_end) FROM report_deliveries").fetchone()
        return row[0]

    def mark_report_sent(self, period_end, recipient):
        with self.conn:
            self.conn.execute(
                "INSERT INTO report_deliveries(period_end, recipient) VALUES (?, ?)",
                (period_end, recipient),
            )

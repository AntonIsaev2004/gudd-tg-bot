"""Однократная загрузка каталога из catalog.json в существующую SQLite-базу."""

import argparse
import json
import sqlite3
from pathlib import Path

from storage import CatalogDB
from pricing import validate_price_text
from catalog_labels import initial_button_text, validate_button_text


ROOT = Path(__file__).parent
MANIFEST = ROOT / "catalog.json"
MEDIA_ROOT = (ROOT / "media").resolve()


def validated_catalog():
    catalog = json.loads(MANIFEST.read_text(encoding="utf-8"))
    batch_id = catalog.get("batch_id")
    items = catalog.get("items")
    if not isinstance(batch_id, str) or not batch_id or not isinstance(items, list) or not items:
        raise ValueError("Некорректный catalog.json")
    source_rows = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Некорректная карточка в catalog.json")
        for key, limit in (("title", 80), ("location", 100), ("rooms", 50),
                           ("teaser", 200), ("description", 800)):
            value = item.get(key)
            if not isinstance(value, str) or len(value) > limit or (not value and key not in ("teaser", "rooms")):
                raise ValueError(f"Некорректное поле {key} в catalog.json")
        for key in ("price", "sale_price"):
            value = item.get(key)
            # Ранее подготовленные каталоги с числовыми ценами тоже поддерживаются.
            if type(value) is int and 0 < value < 10**15:
                value = str(value)
            item[key] = validate_price_text(value)
        source_row = item.get("source_row")
        if type(source_row) is not int or source_row < 2 or source_row in source_rows:
            raise ValueError("Некорректная или повторная исходная строка в catalog.json")
        source_rows.add(source_row)
        for key in ("previous_title", "previous_location", "title_before_address_cleanup"):
            if key in item and (not isinstance(item[key], str) or not item[key] or len(item[key]) > 100):
                raise ValueError(f"Некорректное поле {key} в catalog.json")
        if type(item.get("area")) not in (int, float) or item["area"] <= 0:
            raise ValueError("Некорректная площадь в catalog.json")
        address = item.get("button_address", item["location"])
        if not isinstance(address, str) or not address.strip() or len(address) > 100:
            raise ValueError("Некорректный короткий адрес в catalog.json")
        for field, mode, amount in (("rent_button_text", "rent", item["price"]),
                                     ("sale_button_text", "sale", item["sale_price"])):
            item[field] = validate_button_text(item.get(field, initial_button_text(address, item["area"], amount, mode=mode)))
        features = item.get("features")
        if (not isinstance(features, list) or len(features) > 5 or
                any(not isinstance(value, str) or len(value) > 40 for value in features)):
            raise ValueError("Некорректные особенности в catalog.json")
        photos = item.get("photos")
        if not isinstance(photos, list) or not 1 <= len(photos) <= 10:
            raise ValueError("В карточке должно быть от 1 до 10 фотографий")
        for media in photos:
            if not isinstance(media, str) or not media.startswith("media/"):
                raise ValueError("Некорректный путь к фотографии")
            path = (ROOT / media).resolve()
            if not path.is_relative_to(MEDIA_ROOT) or path.suffix.lower() != ".jpg":
                raise ValueError("Недопустимый путь к фотографии")
            if not path.is_file() or not 0 < path.stat().st_size <= 10_000_000:
                raise ValueError(f"Фотография отсутствует или слишком велика: {media}")
            if not path.read_bytes().startswith(b"\xff\xd8\xff"):
                raise ValueError(f"Фотография не является JPEG: {media}")
    return batch_id, items


def main():
    from bot import load_setting
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Записать объекты в базу данных")
    args = parser.parse_args()
    batch_id, items = validated_catalog()
    configured = load_setting(ROOT / ".env", "DB_PATH") or "data/gudd.sqlite3"
    db_path = Path(configured)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    print(f"Проверено карточек: {len(items)}; база: {db_path}")
    if not args.apply:
        print("Проверка завершена. Для записи в базу запустите с --apply.")
        return
    if not db_path.is_file():
        raise SystemExit("База не найдена. Сначала запустите бота и проверьте DB_PATH.")
    db = CatalogDB(db_path)
    try:
        imported, removed = db.import_catalog(batch_id, items)
        corrected_titles = db.apply_title_corrections(batch_id, items)
        corrected_buttons = db.apply_button_text_defaults(batch_id, items)
        if imported:
            print(f"Каталог обновлён: {len(items)} объектов. ID и история существующих карточек сохранены.")
        else:
            print("Эта партия уже загружена. Повторных карточек нет.")
        print(f"Удалено демонстрационных карточек: {removed} (включая связанные с ними просмотры).")
        if corrected_titles:
            print(f"Названия карточек без адресов обновлены: {corrected_titles}.")
        if corrected_buttons:
            print(f"Тексты кнопок каталога обновлены: {corrected_buttons}.")
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise SystemExit(f"Импорт не выполнен: {exc}") from None

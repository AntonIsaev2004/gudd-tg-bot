"""Однократная загрузка каталога из catalog.json в существующую SQLite-базу."""

import argparse
import json
import sqlite3
from pathlib import Path

from bot import load_setting
from storage import CatalogDB


ROOT = Path(__file__).parent
MANIFEST = ROOT / "catalog.json"
MEDIA_ROOT = (ROOT / "media").resolve()


def validated_catalog():
    catalog = json.loads(MANIFEST.read_text(encoding="utf-8"))
    batch_id = catalog.get("batch_id")
    items = catalog.get("items")
    if not isinstance(batch_id, str) or not batch_id or not isinstance(items, list) or not items:
        raise ValueError("Некорректный catalog.json")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Некорректная карточка в catalog.json")
        for key, limit in (("title", 80), ("location", 100), ("rooms", 50),
                           ("teaser", 200), ("description", 800)):
            value = item.get(key)
            if not isinstance(value, str) or len(value) > limit or (not value and key not in ("teaser", "rooms")):
                raise ValueError(f"Некорректное поле {key} в catalog.json")
        if type(item.get("price")) is not int or item["price"] <= 0:
            raise ValueError("Некорректная аренда в catalog.json")
        if type(item.get("area")) not in (int, float) or item["area"] <= 0:
            raise ValueError("Некорректная площадь в catalog.json")
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
        if db.import_catalog(batch_id, items):
            print(f"Загружено {len(items)} объектов. Демонстрационные карточки скрыты.")
        else:
            print("Эта партия уже загружена. Повторных карточек нет.")
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise SystemExit(f"Импорт не выполнен: {exc}") from None

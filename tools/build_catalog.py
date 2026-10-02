"""Собрать catalog.json и компактные JPEG из исходной таблицы и ZIP с фото.

Запуск на компьютере разработчика (нужны openpyxl и Pillow):
python tools/build_catalog.py '../Объекты для ТГ.xlsx' '/path/to/фото объектов.zip'
"""

import argparse
import json
import re
import zipfile
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parents[1]
FOLDERS = {
    2: "Волочаевская д.8/пом 1",
    3: "Волочаевская д.8/пом 2",
    4: "Волочаевская д.8/пом 3",
    5: "Волочаевская д.8/пом 6",
    6: "Ленинградский п-т д.75 к1Б",
    7: "пр-т Мира д.135А",
    8: "3- Владимировская д. 23",
    9: "ЖК 1-й Нагатинский",
    10: "8 марта",
    11: "Жк Цветочные Поляны",
    12: "ЖК Алхимова",
}
TEASERS = {
    2: "Офис в центре Казани с отдельным входом и высокой проходимостью.",
    3: "Офис на первом этаже с отдельным входом в центре Казани.",
    4: "Готовый арендный бизнес в центре Казани.",
    5: "Офис с отдельным входом и высокой проходимостью.",
    6: "Помещение на первой линии, 100 м от метро «Сокол».",
    7: "Апартаменты бизнес-класса рядом с ВДНХ.",
    8: "Помещение на первом этаже в новом доме рядом с метро.",
    9: "Помещение на первой линии в ЖК «Первый Нагатинский».",
    10: "Помещение под общепит, ритейл или спортзал на первой линии.",
    11: "Помещение с отдельным входом в ЖК «Цветочные Поляны Экопарк».",
    12: "Помещение на первом этаже в ЖК «Алхимово».",
}
# Порядок номеров соответствует алфавитному списку файлов внутри каждой папки.
# Планы с площадью, не совпадающей с таблицей, исключены. Для Ленинградского
# проспекта исключены дублирующий фасад и карта: лимит альбома — 10 фото.
PHOTO_ORDER = {
    2: [3, 1, 2, 4],
    3: [6, 1, 2, 3, 4],
    4: [4, 1, 2, 3, 5],
    5: [6, 7, 1, 2, 3, 4, 5],
    6: [2, 6, 1, 3, 7, 8, 9, 10, 4, 5],
    7: [4, 5, 1, 2, 3, 7, 6],
    8: [5, 6, 1, 2, 3, 7],
    9: [5, 1, 2, 3, 4, 6, 7],
    10: [2, 6, 3, 4, 5, 7],
    11: [1, 2, 4, 3, 5],
    12: [3, 4, 1, 2, 5, 6, 7],
}


def clean_text(value):
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in str(value or "").splitlines()]
    return "\n".join(line for line in lines if line)


def build(xlsx_path, zip_path, include_visualizations):
    sheet = load_workbook(xlsx_path, read_only=True, data_only=True).active
    if sheet.max_row < 12:
        raise ValueError("В таблице нет всех 11 объектов")
    media_root = ROOT / "media"
    media_root.mkdir(exist_ok=True)
    items = []
    with zipfile.ZipFile(zip_path) as archive:
        for row in range(2, 13):
            title, address, area, monthly_rent, payback, description = (
                sheet.cell(row, col).value for col in range(1, 7)
            )
            if not all((title, address, area, monthly_rent, payback, description)):
                raise ValueError(f"Не заполнены данные строки {row}")
            title = clean_text(title)
            location = clean_text(address)
            if 2 <= row <= 5:
                room = (1, 2, 3, 6)[row - 2]
                title += f", пом. {room}"
                location = location.replace("пом.", "пом. ").replace("  ", " ")
                location = location.replace("Волочаевская, 4", "Волочаевская, д. 4")
            else:
                location = location.replace(",135А", ", 135А")
            if row == 10:
                location = location.replace("г.Люберцы", "г. Люберцы").replace("ул.8", "ул. 8")
            description = clean_text(description)
            description = description.replace("г.Казани", "г. Казани")
            description = description.replace("Казани1 этаж", "Казани. 1 этаж")
            description = description.replace("24/7.Электричество", "24/7. Электричество")
            if row == 3:
                description = description.replace("Казани. 1 этаж, -отдельный вход с улицы", "Казани.\n1 этаж, отдельный вход с улицы")
                description = description.replace("-высокая", "Высокая").replace("-остановки", "Остановки").replace("-современные", "Современные")
            if row == 6:
                description = (
                    "Первая линия в исторической части города, высокий пешеходный трафик, "
                    "отличная видимость для автомобильных потоков. Отдельная входная группа. "
                    "Доступ 24/7. Электричество 60 кВт. До станции метро «Сокол» 100 м."
                )
            if row == 8 and description.endswith("«Шоссе Энтузиастов"):
                description += "»"
            if row == 10:
                description = description.replace("1й этаж", "1-й этаж")
                description = description.replace("общественного транспорта новый ЖК", "общественного транспорта, новый ЖК")
            folder = "фото объектов/" + FOLDERS[row] + "/"
            source_files = sorted(
                name for name in archive.namelist() if name.startswith(folder) and not name.endswith("/")
            )
            if not source_files:
                raise ValueError(f"В архиве нет фото для строки {row}: {folder}")
            order = PHOTO_ORDER[row]
            if any(index < 1 or index > len(source_files) for index in order):
                raise ValueError(f"Неверный индекс фото в строке {row}")
            selected = [source_files[index - 1] for index in order]
            if not include_visualizations:
                selected = [name for name in selected if "Gemini_Generated_Image" not in name]
                if row == 10:
                    selected = [name for name in selected if name not in source_files[2:6]]
            if not selected:
                raise ValueError(f"После отбора нет фото для строки {row}")
            if len(selected) > 10:
                raise ValueError(f"Слишком много фото в строке {row}")
            visualizations = any("Gemini_Generated_Image" in name for name in selected) or (
                row == 10 and any(name in source_files[2:6] for name in selected)
            )
            photos = []
            for position, name in enumerate(selected, start=1):
                image = Image.open(BytesIO(archive.read(name)))
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                relative = Path("media") / f"object_{row:02d}" / f"{position:02d}.jpg"
                target = ROOT / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                image.save(target, "JPEG", quality=84, optimize=True)
                if target.stat().st_size > 10_000_000:
                    raise ValueError(f"Фото слишком велико для Telegram: {name}")
                photos.append(relative.as_posix())
            for stale in target.parent.glob("*.jpg"):
                if f"media/object_{row:02d}/{stale.name}" not in photos:
                    stale.unlink()
            teaser = TEASERS[row]
            features = [f"Окупаемость: {payback:g} лет"]
            if visualizations:
                features.append("Часть изображений — визуализации")
            item = {
                "source_row": row,
                "title": title,
                "price": int(monthly_rent),
                "area": float(area),
                "location": location,
                "rooms": "Офис" if row <= 5 else ("Апартаменты" if row == 7 else "Коммерческое помещение"),
                "teaser": teaser,
                "description": description,
                "features": features,
                "photos": photos,
            }
            items.append(item)
    output = ROOT / "catalog.json"
    output.write_text(json.dumps({"batch_id": "catalog-2026-10-02-v1", "items": items}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Готово: {len(items)} объектов, {sum(len(item['photos']) for item in items)} фотографий")
    print(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx", type=Path)
    parser.add_argument("photos_zip", type=Path)
    parser.add_argument("--without-visualizations", action="store_true")
    args = parser.parse_args()
    build(args.xlsx, args.photos_zip, not args.without_visualizations)


if __name__ == "__main__":
    main()

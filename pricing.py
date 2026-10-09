"""Ввод и отображение текстовых цен каталога."""

import re


PRICE_MAX_LENGTH = 50
CURRENCY = re.compile(r"₽|[$€£¥]|\b(?:руб(?:л[а-я]*)?|RUB|USD|EUR)\b", re.IGNORECASE)
MONTH = re.compile(r"\bмес(?:\.|яц[а-я]*)?", re.IGNORECASE)


def validate_price_text(value):
    if not isinstance(value, str):
        raise ValueError("Введите цену текстом, например «71 тыс.» или «10,1 млн.».")
    value = value.strip()
    if not value:
        raise ValueError("Цена не может быть пустой.")
    if len(value) > PRICE_MAX_LENGTH:
        raise ValueError(f"Цена может содержать до {PRICE_MAX_LENGTH} символов.")
    if any(character in value for character in ("\n", "\r", "\t")):
        raise ValueError("Введите цену в одну строку.")
    return value


def price(value):
    text = str(value).strip()
    if text.isdecimal():
        text = f"{int(text):,}".replace(",", " ")
    if any(character.isdigit() for character in text) and not CURRENCY.search(text):
        text += " ₽"
    return text


def monthly_price(value, *, with_vat=False):
    text = price(value)
    has_amount = any(character.isdigit() for character in text)
    if has_amount and not MONTH.search(text):
        text += "/мес."
    if with_vat and has_amount and "ндс" not in text.casefold():
        text += " с НДС"
    return text

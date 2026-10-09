"""Начальное заполнение независимых текстов кнопок каталога."""

import re
from decimal import Decimal

from pricing import monthly_price, price


BUTTON_TEXT_MAX_LENGTH = 100
BUTTON_TEXT_HINT = (
    "Введите весь текст кнопки одной строкой, до 100 символов. "
    "Например: «Москва, ЖК Алхимово · 206,7 м² · 96 млн. ₽». "
    "Бот ничего к нему не добавляет. Длинный текст может обрезаться на телефоне."
)


def validate_button_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Текст кнопки не может быть пустым.")
    value = value.strip()
    if len(value) > BUTTON_TEXT_MAX_LENGTH:
        raise ValueError(f"Текст кнопки может содержать до {BUTTON_TEXT_MAX_LENGTH} символов.")
    if any(ord(character) < 32 for character in value):
        raise ValueError("Введите текст кнопки одной строкой.")
    return value


def compact_price(value):
    """Сокращает только обычное число рублей, без округления и изменения готового текста."""
    text = str(value).strip()
    normalized = re.sub(r"[\s\u00a0]", "", text)
    if not re.fullmatch(r"[0-9]{1,15}(?:[.,][0-9]{1,2})?", normalized):
        return text
    amount = Decimal(normalized.replace(",", "."))
    if amount >= 1_000_000:
        divisor, suffix = Decimal(1_000_000), " млн."
    elif amount >= 1_000:
        divisor, suffix = Decimal(1_000), " тыс."
    else:
        return text
    if amount % divisor:
        number = format(amount / divisor, "f").rstrip("0").rstrip(".")
    else:
        number = str(int(amount / divisor))
    return number.replace(".", ",") + suffix


def initial_button_text(address, area, amount, *, mode="rent"):
    if mode == "sale":
        formatted_price = price(compact_price(amount)) if amount is not None else "Цена по запросу"
    else:
        formatted_price = monthly_price(compact_price(amount))
    return f"{address} · {format(area, 'g').replace('.', ',')} м² · {formatted_price}"

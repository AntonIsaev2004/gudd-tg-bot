"""Общая текстовая шапка карточек с кастомными эмодзи Telegram."""

import html
import json
import re
from dataclasses import dataclass

from field_input import is_skip_command


HEADER_MAX_LENGTH = 240
LOGO_SET_NAME = "GuddCNEmoji"
DEFAULT_HEADER_TEXT = "🏢 Для связи с менеджером: +7 987 404 0101"
CUSTOM_EMOJI_TAG = re.compile(r'<tg-emoji emoji-id="[0-9]+">(.*?)</tg-emoji>', re.DOTALL)


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class CardHeader:
    text: str
    emoji_entities: tuple = ()

    @classmethod
    def from_message(cls, message):
        raw = message.get("text")
        if not isinstance(raw, str):
            raise ValueError("Пришлите шапку одним текстовым сообщением, вставив эмодзи прямо в текст.")
        if is_skip_command(raw):
            return cls("")
        text = raw.strip()
        if not text:
            raise ValueError("Пришлите текст шапки или /skip, чтобы её убрать.")
        if len(text) > HEADER_MAX_LENGTH:
            raise ValueError(f"Шапка может содержать до {HEADER_MAX_LENGTH} символов.")
        if any(ord(character) < 32 and character != "\n" for character in text):
            raise ValueError("В шапке есть недопустимые управляющие символы.")
        removed = utf16_length(raw[:len(raw) - len(raw.lstrip())])
        encoded = text.encode("utf-16-le")
        entities = []
        for entity in message.get("entities", []):
            if entity.get("type") != "custom_emoji":
                continue
            offset, length, emoji_id = entity.get("offset"), entity.get("length"), entity.get("custom_emoji_id")
            if (type(offset) is not int or type(length) is not int or length <= 0
                    or not isinstance(emoji_id, str) or not re.fullmatch(r"[0-9]{1,32}", emoji_id)):
                raise ValueError("Не удалось прочитать эмодзи. Вставьте его заново из набора.")
            offset -= removed
            if offset < 0 or offset + length > utf16_length(text):
                raise ValueError("Эмодзи выходит за границы текста шапки.")
            try:
                encoded[:offset * 2].decode("utf-16-le")
                encoded[offset * 2:(offset + length) * 2].decode("utf-16-le")
            except UnicodeDecodeError:
                raise ValueError("Не удалось прочитать позицию эмодзи.") from None
            entities.append({"type": "custom_emoji", "offset": offset, "length": length, "custom_emoji_id": emoji_id})
        entities.sort(key=lambda entity: entity["offset"])
        if len(entities) > 10 or any(a["offset"] + a["length"] > b["offset"] for a, b in zip(entities, entities[1:])):
            raise ValueError("В шапке слишком много или некорректно расположены эмодзи.")
        return cls(text, tuple(entities))

    def as_html(self):
        encoded = self.text.encode("utf-16-le")
        parts, position = [], 0
        for entity in self.emoji_entities:
            start = entity["offset"] * 2
            end = start + entity["length"] * 2
            parts.append(html.escape(encoded[position:start].decode("utf-16-le")))
            emoji = html.escape(encoded[start:end].decode("utf-16-le"))
            parts.append(f'<tg-emoji emoji-id="{entity["custom_emoji_id"]}">{emoji}</tg-emoji>')
            position = end
        parts.append(html.escape(encoded[position:].decode("utf-16-le")))
        return "".join(parts)

    def to_json(self):
        return json.dumps({"text": self.text, "entities": self.emoji_entities}, ensure_ascii=False)

    @classmethod
    def from_json(cls, value):
        data = json.loads(value)
        return cls("") if data.get("text") == "" else cls.from_message(data)


DEFAULT_HEADER = CardHeader(DEFAULT_HEADER_TEXT)


def without_custom_emoji(params):
    result = dict(params)
    for key in ("text", "caption"):
        if isinstance(result.get(key), str):
            result[key] = CUSTOM_EMOJI_TAG.sub(r"\1", result[key])
    if isinstance(result.get("media"), list):
        result["media"] = [without_custom_emoji(entry) for entry in result["media"]]
    return result


def call_with_emoji_fallback(api, method, **params):
    try:
        return api.call(method, **params)
    except Exception as exc:
        if getattr(exc, "code", None) != 400:
            raise
        fallback = without_custom_emoji(params)
        # После 400 сообщение не отправлено; повтор безопасен. Сетевые ошибки и 429 не повторяем здесь.
        if fallback != params:
            return api.call(method, **fallback)
        raise


def initialize_card_header(api, db):
    """Заполняет шапку один раз, не перезаписывая текст, сохранённый администратором."""
    if db.has_card_header():
        return
    sticker_set = api.call("getStickerSet", name=LOGO_SET_NAME, request_timeout=8)
    if sticker_set.get("sticker_type") != "custom_emoji":
        raise ValueError("Набор логотипа должен быть набором эмодзи.")
    emoji_id = next((sticker.get("custom_emoji_id") for sticker in sticker_set.get("stickers", [])
                     if sticker.get("custom_emoji_id")), None)
    if not emoji_id:
        raise ValueError("В наборе логотипа нет эмодзи.")
    db.set_card_header(CardHeader.from_message({"text": DEFAULT_HEADER_TEXT, "entities": [
        {"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": emoji_id},
    ]}))

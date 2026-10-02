"""Кнопочный каталог недвижимости для Telegram. Python 3.9+, без зависимостей."""

import html
import json
import os
import sqlite3
import ssl
import smtplib
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit

from admin import AdminPanel
from labels import compact_title
from models import Property
from reporting import load_report_settings, send_due_report
from storage import CatalogDB


EXPECTED_BOT_USERNAME = "Gudd_centr_nedvizhimosti_bot"


@dataclass(frozen=True)
class DetailView:
    item_id: int
    media_ids: tuple
    controls_id: int


ACTIVE_DETAILS = {}  # chat_id -> открытая карточка и сообщения, которые нужно убрать


class BotApiError(Exception):
    def __init__(self, code: int, description: str):
        self.code = code
        self.description = description
        super().__init__(f"Telegram API {code}: {description}")


class TelegramAPI:
    def __init__(self, token: str, proxy: str = ""):
        self.base_url = f"https://api.telegram.org/bot{token}/"
        handlers = []
        if proxy:
            try:
                parsed = urlsplit(proxy)
                valid = parsed.scheme in ("http", "https") and bool(parsed.hostname)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("BOT_PROXY должен быть HTTP-прокси, например http://127.0.0.1:7890")
            handlers.append(request.ProxyHandler({"https": proxy}))
        if sys.platform == "darwin" and ssl.get_default_verify_paths().cafile is None:
            system_cafile = Path("/etc/ssl/cert.pem")
            if system_cafile.is_file():
                handlers.append(request.HTTPSHandler(context=ssl.create_default_context(cafile=str(system_cafile))))
        self.opener = request.build_opener(*handlers)

    def call(self, method: str, *, request_timeout: int = 15, **params):
        files = {}
        if method == "sendMediaGroup":
            media = [dict(entry) for entry in params["media"]]
            for index, entry in enumerate(media):
                path = self._local_photo(entry["media"])
                if path:
                    key = f"photo{index}"
                    files[key] = path
                    entry["media"] = f"attach://{key}"
            params["media"] = media
        elif method == "sendPhoto":
            path = self._local_photo(params["photo"])
            if path:
                files["photo"] = path
                params.pop("photo")

        if files:
            boundary = uuid.uuid4().hex
            body = bytearray()
            for name, value in params.items():
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
                body.extend(str(value).encode("utf-8"))
                body.extend(b"\r\n")
            for name, path in files.items():
                body.extend(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
                    'Content-Type: image/jpeg\r\n\r\n'.encode()
                )
                body.extend(path.read_bytes())
                body.extend(b"\r\n")
            body.extend(f'--{boundary}--\r\n'.encode())
            body = bytes(body)
            content_type = f"multipart/form-data; boundary={boundary}"
        else:
            body = json.dumps(params, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        req = request.Request(
            self.base_url + method,
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with self.opener.open(req, timeout=request_timeout) as response:
                payload = json.load(response)
        except error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except (ValueError, OSError):
                raise BotApiError(exc.code, "ошибка HTTP") from None
            raise BotApiError(payload.get("error_code", exc.code), payload.get("description", "ошибка API")) from None
        if not payload.get("ok"):
            raise BotApiError(payload.get("error_code", 0), payload.get("description", "ошибка API"))
        return payload["result"]

    @staticmethod
    def _local_photo(value):
        if not isinstance(value, str) or not value.startswith("media/"):
            return None
        media_root = (Path(__file__).parent / "media").resolve()
        path = (Path(__file__).parent / value).resolve()
        if not path.is_relative_to(media_root) or path.suffix.lower() != ".jpg":
            raise ValueError("Недопустимый путь к фотографии")
        if not path.is_file() or path.stat().st_size > 10_000_000:
            raise OSError(f"Фотография отсутствует или слишком велика: {value}")
        return path


def button(label: str, data: str):
    return {"text": label, "callback_data": data}


def price(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def format_area(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def catalog(db: CatalogDB):
    rows = []
    for item in db.list_properties():
        action = f"show:{item.id}"
        rows.append([button(compact_title(item.title), action)])
        rows.append([button(f"↳ {format_area(item.area)} м² · {price(item.price)}", action)])
    if not rows:
        return "🏠 Пока нет доступных объектов.", None
    return "🏠 Объекты · аренда в месяц", {"inline_keyboard": rows}


def detail_controls(db: CatalogDB, item: Property):
    items = db.list_properties()
    index = next(i for i, candidate in enumerate(items) if candidate.id == item.id)
    rows = []
    if len(items) > 1:
        previous_item = items[(index - 1) % len(items)]
        next_item = items[(index + 1) % len(items)]
        rows.append([button("← Пред.", f"view:{previous_item.id}"), button("След. →", f"view:{next_item.id}")])
    rows.extend([
        [button("← К списку", f"back:{item.id}")],
        [{"text": "💬 Связаться с менеджером", "url": "https://t.me/gudd_manager"}],
    ])
    return f"Объект {index + 1} из {len(items)}", {"inline_keyboard": rows}


def detail_caption(item: Property, photo_unavailable: bool = False):
    amount = f"Аренда: {price(item.price)}/мес. с НДС"
    lines = [
        f"<b>{html.escape(item.title)}</b>",
        f"<b>{amount}</b>",
        "",
        f"📍 {html.escape(item.location)}",
        f"📐 {format_area(item.area)} м²" + (f"  ·  🏢 {html.escape(item.rooms)}" if item.rooms else ""),
        "",
        html.escape(item.description),
        "",
    ]
    if item.features:
        lines.extend(("", " · ".join(html.escape(feature) for feature in item.features)))
    if photo_unavailable:
        lines.append("<i>Фото временно недоступны.</i>")
    return "\n".join(lines)


def send_catalog(api: TelegramAPI, db: CatalogDB, chat_id: int):
    text, markup = catalog(db)
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if markup:
        params["reply_markup"] = markup
    return api.call("sendMessage", **params)


def send_detail(api: TelegramAPI, chat_id: int, item: Property):
    caption = detail_caption(item)
    try:
        if len(item.photos) >= 2:
            media = [{"type": "photo", "media": photo_url} for photo_url in item.photos[:10]]
            media[0].update(caption=caption, parse_mode="HTML")
            return api.call("sendMediaGroup", chat_id=chat_id, media=media, request_timeout=60)
        if item.photos:
            return [api.call("sendPhoto", chat_id=chat_id, photo=item.photos[0], caption=caption, parse_mode="HTML", request_timeout=30)]
    except (BotApiError, OSError, ValueError) as exc:
        print(f"Альбом объекта {item.id} недоступен ({type(exc).__name__}); пробую обложку.", file=sys.stderr)
        try:
            return [api.call("sendPhoto", chat_id=chat_id, photo=item.photos[0], caption=caption, parse_mode="HTML", request_timeout=30)]
        except (BotApiError, OSError, ValueError):
            pass
    return [api.call("sendMessage", chat_id=chat_id, text=detail_caption(item, photo_unavailable=True), parse_mode="HTML")]


def delete_quietly(api: TelegramAPI, chat_id: int, message_id: int):
    try:
        api.call("deleteMessage", chat_id=chat_id, message_id=message_id)
    except BotApiError:
        pass


def delete_detail(api: TelegramAPI, chat_id: int, detail: DetailView):
    for message_id in detail.media_ids:
        delete_quietly(api, chat_id, message_id)
    delete_quietly(api, chat_id, detail.controls_id)


def show_detail(api: TelegramAPI, db: CatalogDB, chat_id: int, item: Property, source_message_id: int):
    sent = send_detail(api, chat_id, item)
    media_ids = tuple(message["message_id"] for message in sent)
    text, markup = detail_controls(db, item)
    try:
        controls = api.call("sendMessage", chat_id=chat_id, text=text, reply_markup=markup)
    except BotApiError:
        for message_id in media_ids:
            delete_quietly(api, chat_id, message_id)
        raise

    previous = ACTIVE_DETAILS.get(chat_id)
    ACTIVE_DETAILS[chat_id] = DetailView(item.id, media_ids, controls["message_id"])
    if previous:
        delete_detail(api, chat_id, previous)
    if not previous or source_message_id != previous.controls_id:
        delete_quietly(api, chat_id, source_message_id)


def handle_message(api: TelegramAPI, db: CatalogDB, admin: AdminPanel, message: dict):
    chat = message.get("chat", {})
    if chat.get("type") != "private":
        return
    user_id = db.upsert_user(message.get("from"))
    raw_text = (message.get("text") or "").strip()
    command = raw_text.split(maxsplit=1)[0].split("@", 1)[0] if raw_text else ""
    chat_id = chat["id"]
    if command == "/id":
        api.call("sendMessage", chat_id=chat_id, text=f"Ваш Telegram ID: {user_id}")
        return
    if admin.handle_message(message):
        return
    if command in ("/start", "/catalog"):
        send_catalog(api, db, chat_id)
        db.record_event(user_id, "bot_started" if command == "/start" else "catalog_opened")
        previous = ACTIVE_DETAILS.pop(chat_id, None)
        if previous:
            delete_detail(api, chat_id, previous)
    elif command == "/help":
        lines = ["/start или /catalog — список объектов", "/help — список команд", "/id — ваш Telegram ID"]
        if admin.is_admin(user_id):
            lines.extend(("/admin — управление объектами", "/cancel — отмена ввода в админке",
                          "/done — завершить загрузку фото", "/skip — пропустить необязательное поле"))
        api.call("sendMessage", chat_id=chat_id, text="\n".join(lines))
    elif command.startswith("/"):
        api.call("sendMessage", chat_id=chat_id, text="Откройте каталог командой /start или /catalog. Команды: /help.")


def handle_callback(api: TelegramAPI, db: CatalogDB, admin: AdminPanel, query: dict):
    query_id = query["id"]
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or "message_id" not in message:
        api.call("answerCallbackQuery", callback_query_id=query_id, text="Откройте бота в личном чате.", show_alert=True)
        return

    user_id = db.upsert_user(query.get("from"))
    if admin.handle_callback(query):
        return
    data = query.get("data", "")
    parts = data.split(":")
    chat_id = chat["id"]
    message_id = message["message_id"]
    previous = ACTIVE_DETAILS.get(chat_id)
    if (len(parts) == 2 and parts[0] in ("view", "back") and previous
            and message_id != previous.controls_id):
        api.call("answerCallbackQuery", callback_query_id=query_id, text="Эта карточка устарела. Откройте актуальную.")
        return

    if ((len(parts) == 2 and parts[0] in ("show", "view") and parts[1].isdigit()) or
            (len(parts) == 4 and parts[0] in ("show", "open") and all(part.isdigit() for part in parts[1:]))):
        item = db.get_property(int(parts[1]))
        if item is None:
            api.call("answerCallbackQuery", callback_query_id=query_id, text="Объект больше не доступен.", show_alert=True)
            return
    else:
        item = None

    api.call("answerCallbackQuery", callback_query_id=query_id)
    if len(parts) == 2 and parts[0] == "catalog" and parts[1].isdigit():
        text, markup = catalog(db)
        params = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if markup:
            params["reply_markup"] = markup
        api.call("editMessageText", **params)
    elif len(parts) == 2 and parts[0] == "back" and parts[1].isdigit():
        send_catalog(api, db, chat_id)
        db.record_event(user_id, "catalog_opened")
        previous = ACTIVE_DETAILS.pop(chat_id, None)
        if previous:
            delete_detail(api, chat_id, previous)
        else:
            delete_quietly(api, chat_id, message_id)
    elif item is not None:
        show_detail(api, db, chat_id, item, message_id)
        db.record_event(user_id, "property_opened", item.id)


def load_setting(env_file: Path, name: str) -> str:
    """Читает настройку из .env рядом со скриптом; env-переменная — запасной вариант."""
    try:
        lines = env_file.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError:
        raise SystemExit(f"Не удалось прочитать {env_file.name}.") from None

    for line in lines:
        key, separator, value = line.strip().partition("=")
        if separator and key.strip() == name:
            setting = value.strip()
            if len(setting) >= 2 and setting[0] in ("'", '"') and setting[-1] == setting[0]:
                setting = setting[1:-1]
            if setting:
                return setting
    return os.environ.get(name, "").strip()


def load_token(env_file: Path) -> str:
    return load_setting(env_file, "BOT_TOKEN")


def parse_admin_ids(raw: str):
    if not raw:
        return set()
    try:
        ids = {int(value.strip()) for value in raw.split(",")}
    except ValueError:
        raise SystemExit("ADMIN_IDS должен содержать Telegram ID через запятую.") from None
    if any(value <= 0 for value in ids):
        raise SystemExit("ADMIN_IDS должен содержать положительные Telegram ID.")
    return ids


def main():
    env_file = Path(__file__).with_name(".env")
    token = load_token(env_file)
    if not token:
        raise SystemExit("Укажите BOT_TOKEN в файле .env рядом с bot.py.")
    try:
        api = TelegramAPI(token, proxy=load_setting(env_file, "BOT_PROXY"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print("Проверяю подключение к Telegram API...", flush=True)
    try:
        me = api.call("getMe", request_timeout=8)
    except BotApiError as exc:
        raise SystemExit(f"Не удалось подключиться к Telegram: {exc.description}") from None
    except error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise SystemExit("Не удалось проверить сертификат Telegram API. Проверьте сертификаты Python и VPN.") from None
        raise SystemExit("Нет соединения с api.telegram.org. Включите VPN или укажите BOT_PROXY в .env.") from None
    except (TimeoutError, OSError):
        raise SystemExit("Нет соединения с api.telegram.org. Включите VPN или укажите BOT_PROXY в .env.") from None
    if me.get("username", "").casefold() != EXPECTED_BOT_USERNAME.casefold():
        raise SystemExit(
            f"BOT_TOKEN относится к @{me.get('username', 'другому боту')}. "
            f"Укажите токен @{EXPECTED_BOT_USERNAME} в bot/.env."
        )
    configured_db_path = load_setting(env_file, "DB_PATH") or "data/gudd.sqlite3"
    db_path = Path(configured_db_path)
    if not db_path.is_absolute():
        db_path = Path(__file__).parent / db_path
    try:
        db = CatalogDB(db_path)
    except (OSError, sqlite3.Error):
        raise SystemExit(f"Не удалось открыть базу данных: {db_path}") from None
    try:
        report_settings = load_report_settings(env_file, load_setting)
    except ValueError as exc:
        db.close()
        raise SystemExit(str(exc)) from None
    admin = AdminPanel(api, db, parse_admin_ids(load_setting(env_file, "ADMIN_IDS")))
    print(f"Бот @{me['username']} запущен. Откройте его и отправьте /start. Ctrl+C — остановить.", flush=True)
    if not admin.admin_ids:
        print("Админка закрыта: укажите ADMIN_IDS в .env (свой ID покажет команда /id).", flush=True)
    if report_settings is None:
        print("Почтовая сводка отключена: заполните SMTP_HOST, SMTP_USER и SMTP_PASSWORD в .env.", flush=True)
    else:
        print(f"Почтовая сводка: понедельник и четверг 10:00 МСК → {report_settings.recipient}.", flush=True)
    offset = None
    next_report_check = 0.0
    try:
        while True:
            if report_settings is not None and time.monotonic() >= next_report_check:
                try:
                    sent_period_end = send_due_report(db, report_settings)
                except (smtplib.SMTPException, OSError, sqlite3.Error, ValueError) as exc:
                    print(f"Не удалось отправить почтовую сводку ({type(exc).__name__}). Повтор через час.", file=sys.stderr)
                    next_report_check = time.monotonic() + 3600
                else:
                    if sent_period_end is not None:
                        print(f"Почтовая сводка до {sent_period_end} отправлена.", flush=True)
                    next_report_check = time.monotonic() + 60
            try:
                params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    params["offset"] = offset
                updates = api.call("getUpdates", request_timeout=35, **params)
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        if "message" in update:
                            handle_message(api, db, admin, update["message"])
                        elif "callback_query" in update:
                            handle_callback(api, db, admin, update["callback_query"])
                    except BotApiError as exc:
                        print(f"Не удалось обработать update {update['update_id']}: {exc.description}", file=sys.stderr)
            except BotApiError as exc:
                if exc.code == 409:
                    raise SystemExit("Конфликт polling: другой процесс бота уже запущен или настроен webhook.") from None
                print(f"Ошибка Telegram API ({exc.code}). Повтор через 3 секунды.", file=sys.stderr)
                time.sleep(3)
            except (error.URLError, TimeoutError, OSError):
                print("Нет соединения с Telegram. Повтор через 3 секунды.", file=sys.stderr)
                time.sleep(3)
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nБот остановлен.")

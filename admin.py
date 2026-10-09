"""Простая админка каталога внутри личного чата с ботом."""

import html
import re
import secrets
from dataclasses import dataclass, field, replace

from models import Property
from pricing import monthly_price, price, validate_price_text


ADDRESS_BUTTON_HINT = (
    "Этот адрес будет на кнопке в каталоге рядом с площадью и ценой. "
    "Длинный текст может обрезаться на телефоне; полного адреса это не меняет."
)
PRICE_INPUT_HINT = "Введите цену текстом до 50 символов, например «71 тыс.», «10,1 млн.» или «по запросу»."

STEPS = (
    ("title", "Название объекта (до 80 символов):"),
    ("price", "Аренда в месяц с НДС (текст до 50 символов, например «71 тыс.»):"),
    ("sale_price", "Цена продажи (текст до 50 символов, например «10,1 млн.»):"),
    ("area", "Площадь в м² (например, 82 или 82,5):"),
    ("location", f"Адрес объекта (до 100 символов):\n{ADDRESS_BUTTON_HINT}"),
    ("rooms", "Тип помещения (например, «офис»):"),
    ("teaser", "Краткое описание (до 200 символов). Если не нужно — /skip:"),
    ("description", "Полное описание для карточки (до 800 символов):"),
    ("features", "Особенности через запятую (до 5 пунктов). Если не нужны — /skip:"),
)
FIELD_LABELS = {
    "title": "Название", "price": "Аренда в месяц", "sale_price": "Цена продажи", "area": "Площадь", "location": "Адрес",
    "rooms": "Тип помещения", "teaser": "Краткое описание", "description": "Описание",
    "features": "Особенности",
}
MAX_LENGTHS = {"title": 80, "location": 100, "rooms": 50, "teaser": 200, "description": 800}


@dataclass
class AdminState:
    mode: str
    step: int = 0
    values: dict = field(default_factory=dict)
    photos: list = field(default_factory=list)
    item_id: int = 0
    field_name: str = ""
    token: str = ""


def button(label, data):
    return {"text": label, "callback_data": data}


def parse_value(name, raw):
    value = raw.strip()
    if name in ("price", "sale_price"):
        return validate_price_text(value)
    if name == "area":
        normalized = re.sub(r"[\s\u00a0]", "", value).replace(",", ".")
        if len(normalized) > 10 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", normalized) or not 0 < float(normalized) < 10**6:
            raise ValueError("Введите площадь числом, например 82 или 82,5.")
        return float(normalized)
    if name == "features":
        if value == "/skip":
            return ()
        features = tuple(part.strip() for part in re.split(r"[,\n]", value) if part.strip())
        if len(features) > 5 or any(len(part) > 40 for part in features):
            raise ValueError("Можно указать до 5 особенностей, каждая — до 40 символов.")
        return features
    if name == "teaser" and value == "/skip":
        return ""
    if not value:
        raise ValueError("Поле не может быть пустым.")
    if len(value) > MAX_LENGTHS[name]:
        raise ValueError(f"Слишком длинный текст. Максимум {MAX_LENGTHS[name]} символов.")
    return value


class AdminPanel:
    def __init__(self, api, db, admin_ids, preview_sender):
        self.api = api
        self.db = db
        self.admin_ids = set(admin_ids)
        self.preview_sender = preview_sender
        self.states = {}

    def is_admin(self, user_id):
        return user_id in self.admin_ids

    def _send(self, chat_id, text, rows=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if rows:
            params["reply_markup"] = {"inline_keyboard": rows}
        return self.api.call("sendMessage", **params)

    def menu(self, chat_id):
        self._send(chat_id, "⚙️ Управление объектами", [
            [button("🏠 Список объектов", "adm:list")],
            [button("➕ Добавить объект", "adm:add")],
        ])

    def preview_item(self, chat_id, item, *, draft=False, mode="rent"):
        label = "Черновик — ещё не виден посетителям." if draft else "Предпросмотр карточки."
        self._send(chat_id, f"👁 {label}")
        self.preview_sender(self.api, chat_id, item, mode=mode)
        state = self.states.get(chat_id) if draft else None
        prefix = f"adm:previewdraft:{state.token}" if state else f"adm:preview:{item.id}"
        self._send(chat_id, "Предпросмотр цены:", [
            [button("Аренда", f"{prefix}:rent"), button("Продажа", f"{prefix}:sale")]
        ])
        if draft:
            state = self.states.get(chat_id)
            if state and state.mode == "confirm_create":
                self._send(chat_id, "Карточка ещё не сохранена.", [
                    [button("✅ Сохранить", f"adm:save:{state.token}"), button("❌ Отмена", "adm:cancel")]
                ])
            else:
                self._next_create_prompt(chat_id, state)
        else:
            self._send(chat_id, "Вернуться к управлению объектом:", [
                [button("🖼 Фото", f"adm:photos:{item.id}"), button("← К объекту", f"adm:edit:{item.id}")]
            ])

    @staticmethod
    def draft_property(state):
        values = state.values
        return Property(
            0, values["title"], values["price"], values["area"], values["location"],
            values["rooms"], values["teaser"], values["description"],
            tuple(values["features"]), tuple(state.photos),
            sale_price=values["sale_price"],
        )

    def list_menu(self, chat_id):
        rows = []
        for item in self.db.list_properties(active_only=False):
            icon = "✅" if self.db.is_active(item.id) else "🙈"
            rows.append([button(f"{icon} #{item.id} {item.title}", f"adm:edit:{item.id}")])
        rows.append([button("➕ Добавить", "adm:add"), button("← Меню", "adm:menu")])
        self._send(chat_id, "Объекты (🙈 — скрыт):", rows)

    def edit_menu(self, chat_id, item_id):
        item = self.db.get_property(item_id, active_only=False)
        if item is None:
            self._send(chat_id, "Объект уже удалён.")
            self.list_menu(chat_id)
            return
        rows = [
            [button("Название", f"adm:field:{item_id}:title"), button("Адрес", f"adm:field:{item_id}:location")],
            [button("Аренда в месяц", f"adm:field:{item_id}:price"), button("Цена продажи", f"adm:field:{item_id}:sale_price")],
            [button("Площадь", f"adm:field:{item_id}:area")],
            [button("Тип помещения", f"adm:field:{item_id}:rooms"), button("Краткое описание", f"adm:field:{item_id}:teaser")],
            [button("Описание", f"adm:field:{item_id}:description"), button("Особенности", f"adm:field:{item_id}:features")],
            [button("👁 Предпросмотр", f"adm:preview:{item_id}")],
            [button(f"🖼 Фото ({len(item.photos)})", f"adm:photos:{item_id}")],
            [button("Показать" if not self.db.is_active(item_id) else "Скрыть", f"adm:toggle:{item_id}")],
            [button("↑ Выше", f"adm:up:{item_id}"), button("↓ Ниже", f"adm:down:{item_id}")],
            [button("🗑 Удалить объект", f"adm:delete:{item_id}")],
            [button("← Список", "adm:list")],
        ]
        description = html.escape(item.description)
        amount = html.escape(f"Аренда: {monthly_price(item.price, with_vat=True)}")
        sale = html.escape(f"Продажа: {price(item.sale_price)}") if item.sale_price else "Продажа: цена по запросу"
        self._send(
            chat_id,
            f"<b>#{item.id} {html.escape(item.title)}</b>\n"
            f"{amount} · {format(item.area, 'g').replace('.', ',')} м²\n"
            f"{sale}\n"
            f"{html.escape(item.location)} · {html.escape(item.rooms)}\n"
            f"Статус: {'показывается' if self.db.is_active(item_id) else 'скрыт'}\n\n"
            f"Кратко: {html.escape(item.teaser or '—')}\n"
            f"Описание: {description}\n"
            f"Особенности: {html.escape(', '.join(item.features) or '—')}",
            rows,
        )

    def photo_menu(self, chat_id, item_id):
        item = self.db.get_property(item_id, active_only=False)
        if item is None:
            self.list_menu(chat_id)
            return
        rows = [[button("➕ Добавить фото", f"adm:addphoto:{item_id}")]]
        rows.append([button("👁 Вся карточка", f"adm:preview:{item_id}")])
        rows.extend(
            [button(f"Удалить фото {index}", f"adm:photodel:{item_id}:{photo['id']}")]
            for index, photo in enumerate(self.db.list_photos(item_id), start=1)
        )
        rows.append([button("← К объекту", f"adm:edit:{item_id}")])
        self._send(chat_id, f"🖼 Фото объекта #{item_id}: {len(item.photos)}/10\nПорядок — как при загрузке.", rows)

    def _next_create_prompt(self, chat_id, state):
        if state.step < len(STEPS):
            self._send(chat_id, f"Шаг {state.step + 1}/{len(STEPS)}. {STEPS[state.step][1]}\n/cancel — отмена")
        else:
            rows = [[button("👁 Предпросмотр", f"adm:previewdraft:{state.token}")]] if state.photos else None
            self._send(chat_id, "Пришлите от 1 до 10 фотографий сообщениями. После последней отправьте /done.\n/cancel — отмена", rows)

    def handle_message(self, message):
        chat_id = message["chat"]["id"]
        actor_id = (message.get("from") or {}).get("id")
        raw = (message.get("text") or "").strip()
        command = raw.split(maxsplit=1)[0].split("@", 1)[0] if raw else ""
        if command == "/admin":
            if not self.is_admin(actor_id):
                self._send(chat_id, "Нет доступа к админке.")
                return True
            self.states.pop(chat_id, None)
            self.menu(chat_id)
            return True
        if not self.is_admin(actor_id):
            return False
        if command in ("/start", "/catalog", "/help"):
            self.states.pop(chat_id, None)
            return False
        state = self.states.get(chat_id)
        if state is None:
            return False
        if command == "/cancel":
            self.states.pop(chat_id, None)
            self.menu(chat_id)
            return True
        if state.mode == "create":
            if state.step < len(STEPS):
                name = STEPS[state.step][0]
                try:
                    state.values[name] = parse_value(name, raw)
                except ValueError as exc:
                    self._send(chat_id, html.escape(str(exc)))
                    self._next_create_prompt(chat_id, state)
                    return True
                state.step += 1
                self._next_create_prompt(chat_id, state)
                return True
            if message.get("photo"):
                if len(state.photos) >= 10:
                    self._send(chat_id, "Максимум 10 фотографий. Отправьте /done.")
                else:
                    state.photos.append(message["photo"][-1]["file_id"])
                    self._send(chat_id, f"Фото добавлено: {len(state.photos)}/10. Пришлите следующее или /done.", [
                        [button("👁 Предпросмотр", f"adm:previewdraft:{state.token}")]
                    ])
                return True
            if command == "/done":
                if not state.photos:
                    self._send(chat_id, "Нужна хотя бы одна фотография.")
                    return True
                state.mode = "confirm_create"
                self._send(chat_id, f"Создать «{html.escape(state.values['title'])}» с {len(state.photos)} фото?", [
                    [button("👁 Предпросмотр", f"adm:previewdraft:{state.token}")],
                    [button("✅ Сохранить", f"adm:save:{state.token}"), button("❌ Отмена", "adm:cancel")]
                ])
                return True
            self._next_create_prompt(chat_id, state)
            return True
        if state.mode == "edit_field":
            try:
                value = parse_value(state.field_name, raw)
            except ValueError as exc:
                self._send(chat_id, html.escape(str(exc)))
                return True
            updated = self.db.update_property(state.item_id, state.field_name, value)
            self.states.pop(chat_id, None)
            if updated:
                self._send(chat_id, "Сохранено.")
                self.edit_menu(chat_id, state.item_id)
            else:
                self._send(chat_id, "Объект уже удалён.")
            return True
        if state.mode == "add_photo":
            if message.get("photo"):
                if not self.db.get_property(state.item_id, active_only=False):
                    self.states.pop(chat_id, None)
                    self._send(chat_id, "Объект уже удалён.")
                    return True
                if self.db.add_photo(state.item_id, message["photo"][-1]["file_id"]):
                    self._send(chat_id, "Фото добавлено. Пришлите ещё или /done.")
                else:
                    self._send(chat_id, "Достигнут лимит 10 фото. Отправьте /done.")
                return True
            if command == "/done":
                self.states.pop(chat_id, None)
                self.photo_menu(chat_id, state.item_id)
                return True
            self._send(chat_id, "Пришлите фотографию или /done для завершения.")
            return True
        self._send(chat_id, "Нажмите «Сохранить» или «Отмена» под предыдущим сообщением.")
        return True

    def handle_callback(self, query):
        data = query.get("data", "")
        if not data.startswith("adm:"):
            return False
        chat_id = query["message"]["chat"]["id"]
        actor_id = (query.get("from") or {}).get("id")
        if not self.is_admin(actor_id):
            self.api.call("answerCallbackQuery", callback_query_id=query["id"], text="Нет доступа.", show_alert=True)
            return True
        self.api.call("answerCallbackQuery", callback_query_id=query["id"])
        parts = data.split(":")
        action = parts[1] if len(parts) >= 2 else ""
        item_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
        if action == "menu":
            self.menu(chat_id)
        elif action == "list":
            self.list_menu(chat_id)
        elif action == "add":
            state = AdminState("create", token=secrets.token_hex(4))
            self.states[chat_id] = state
            self._next_create_prompt(chat_id, state)
        elif action == "cancel":
            self.states.pop(chat_id, None)
            self.menu(chat_id)
        elif action == "previewdraft":
            state = self.states.get(chat_id)
            if (state and state.mode in ("create", "confirm_create")
                    and state.step == len(STEPS) and state.photos
                    and len(parts) in (3, 4) and parts[2] == state.token
                    and (len(parts) == 3 or parts[3] in ("rent", "sale"))):
                self.preview_item(chat_id, self.draft_property(state), draft=True, mode=parts[3] if len(parts) == 4 else "rent")
            else:
                self._send(chat_id, "Черновик уже недоступен.")
        elif action == "save":
            state = self.states.get(chat_id)
            if state and state.mode == "confirm_create" and len(parts) == 3 and parts[2] == state.token:
                item_id = self.db.create_property(state.values, state.photos)
                self.states.pop(chat_id, None)
                self._send(chat_id, f"Объект #{item_id} создан.")
                self.edit_menu(chat_id, item_id)
            else:
                self._send(chat_id, "Эта операция уже завершена.")
        elif item_id is None:
            self._send(chat_id, "Не удалось определить объект.")
        elif action == "edit":
            self.edit_menu(chat_id, item_id)
        elif action == "preview" and (len(parts) == 3 or (len(parts) == 4 and parts[3] in ("rent", "sale"))):
            item = self.db.get_property(item_id, active_only=False)
            if item:
                self.preview_item(chat_id, item, mode=parts[3] if len(parts) == 4 else "rent")
            else:
                self._send(chat_id, "Объект уже удалён.")
        elif action == "field" and len(parts) == 4 and parts[3] in FIELD_LABELS:
            if self.db.get_property(item_id, active_only=False):
                self.states[chat_id] = AdminState("edit_field", item_id=item_id, field_name=parts[3])
                hint = f"\n{ADDRESS_BUTTON_HINT}" if parts[3] == "location" else ""
                if parts[3] in ("price", "sale_price"):
                    hint = f"\n{PRICE_INPUT_HINT}"
                self._send(chat_id, f"Новое значение поля «{FIELD_LABELS[parts[3]]}»:{hint}\n/cancel — отмена")
        elif action == "photos":
            self.photo_menu(chat_id, item_id)
        elif action == "addphoto":
            if self.db.get_property(item_id, active_only=False):
                self.states[chat_id] = AdminState("add_photo", item_id=item_id)
                self._send(chat_id, "Пришлите новые фотографии (до 10 всего). После последней отправьте /done.\n/cancel — отмена")
        elif action == "photodel" and len(parts) == 4 and parts[3].isdigit():
            item = self.db.get_property(item_id, active_only=False)
            photos = self.db.list_photos(item_id) if item else []
            selected = next(((index, photo) for index, photo in enumerate(photos, 1)
                             if photo["id"] == int(parts[3])), None)
            if selected:
                index, photo = selected
                caption = f"Фото {index} из {len(photos)} · {html.escape(item.title)}"
                self.preview_sender(self.api, chat_id, replace(item, photos=(photo["media"],)), caption=caption)
                self._send(chat_id, f"Удалить фото {index}?", [
                    [button("Да, удалить", f"adm:confirmphotodel:{item_id}:{parts[3]}"),
                     button("Отмена", f"adm:photos:{item_id}")]
                ])
        elif action == "confirmphotodel" and len(parts) == 4 and parts[3].isdigit():
            self.db.remove_photo(item_id, int(parts[3]))
            self.photo_menu(chat_id, item_id)
        elif action == "toggle":
            self.db.set_active(item_id, not self.db.is_active(item_id))
            self.edit_menu(chat_id, item_id)
        elif action in ("up", "down"):
            self.db.move_property(item_id, -1 if action == "up" else 1)
            self.edit_menu(chat_id, item_id)
        elif action == "delete":
            item = self.db.get_property(item_id, active_only=False)
            if item:
                self._send(chat_id, f"Удалить «{html.escape(item.title)}» без возможности отмены?", [
                    [button("Да, удалить", f"adm:confirmdelete:{item_id}"), button("Отмена", f"adm:edit:{item_id}")]
                ])
        elif action == "confirmdelete":
            self.db.delete_property(item_id)
            self._send(chat_id, f"Объект #{item_id} удалён.")
            self.list_menu(chat_id)
        return True

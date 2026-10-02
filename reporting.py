"""Сводки по понедельникам и четвергам и отправка через SMTP с TLS."""

import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo("Europe/Moscow")
REPORT_TIME = time(hour=10)


@dataclass(frozen=True)
class ReportSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipient: str


def _email_address(value):
    return bool(value and "\r" not in value and "\n" not in value and
                parseaddr(value)[1] == value and "@" in value)


def load_report_settings(env_file, load_setting):
    host = load_setting(env_file, "SMTP_HOST")
    username = load_setting(env_file, "SMTP_USER")
    password = load_setting(env_file, "SMTP_PASSWORD")
    if not any((host, username, password)):
        return None
    if not all((host, username, password)):
        raise ValueError("Для почтовой сводки заполните SMTP_HOST, SMTP_USER и SMTP_PASSWORD в .env.")
    try:
        port = int(load_setting(env_file, "SMTP_PORT") or "465")
    except ValueError:
        raise ValueError("SMTP_PORT должен быть числом от 1 до 65535.") from None
    if not 1 <= port <= 65535:
        raise ValueError("SMTP_PORT должен быть числом от 1 до 65535.")
    sender = load_setting(env_file, "SMTP_FROM") or username
    recipient = load_setting(env_file, "REPORT_TO")
    if not recipient:
        raise ValueError("Для почтовой сводки укажите REPORT_TO в .env или переменных окружения.")
    if not _email_address(sender) or not _email_address(recipient):
        raise ValueError("SMTP_FROM и REPORT_TO должны быть адресами почты.")
    return ReportSettings(host, port, username, password, sender, recipient)


def _utc_db_time(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _local_db_time(value):
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    ).astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")


def _one_line(value):
    return " ".join(str(value or "").split()) or "—"


def _count(value, singular, few, many):
    form = many if value % 100 in (11, 12, 13, 14) else (
        singular if value % 10 == 1 else few if value % 10 in (2, 3, 4) else many
    )
    return f"{value} {form}"


def report_content(db, start_local, end_local):
    start_utc = _utc_db_time(start_local)
    end_utc = _utc_db_time(end_local)
    properties, visits = db.report_data(start_utc, end_utc)

    titles = {row["id"]: _one_line(row["title"]) for row in properties}
    by_property = {row["id"]: [] for row in properties}
    by_visitor = {}
    for row in visits:
        item_id = row["property_id"]
        by_property.setdefault(item_id, []).append(row)
        key = (item_id, row["telegram_id"])
        if key not in by_visitor:
            by_visitor[key] = {"row": row, "first": row["created_at"], "last": row["created_at"], "count": 0}
        by_visitor[key]["last"] = row["created_at"]
        by_visitor[key]["count"] += 1

    subject = f"GUDD: сводка {start_local:%d.%m %H:%M}–{end_local:%d.%m.%Y %H:%M} МСК"
    unique_visitors = {row["telegram_id"] for row in visits}
    lines = [
        f"Сводка за {start_local:%d.%m.%Y %H:%M}–{end_local:%d.%m.%Y %H:%M} (МСК)",
        f"Просмотров объектов: {len(visits)}",
        f"Уникальных посетителей: {len(unique_visitors)}",
        "",
        "Объекты:",
    ]
    for item_id, rows in by_property.items():
        visitor_count = len({row["telegram_id"] for row in rows})
        title = titles.get(item_id, "(объект удалён)")
        lines.append(
            f"{title}: "
            f"{_count(len(rows), 'просмотр', 'просмотра', 'просмотров')}, "
            f"{_count(visitor_count, 'посетитель', 'посетителя', 'посетителей')}"
        )

    lines.extend(("", "Посетители по объектам:"))
    if not by_visitor:
        lines.append("За период просмотров не было.")
    for (item_id, user_id), entry in by_visitor.items():
        row = entry["row"]
        full_name = " ".join(
            part for part in (_one_line(row["first_name"]), _one_line(row["last_name"])) if part != "—"
        ) or "Имя не указано"
        username = f"@{_one_line(row['username'])}" if row["username"] else "ник не указан"
        lines.append(
            f"{titles.get(item_id, '(объект удалён)')} · {full_name} · {username} · Telegram ID {user_id} · "
            f"просмотров {entry['count']} · первый {_local_db_time(entry['first'])} · "
            f"последний {_local_db_time(entry['last'])}"
        )
    return subject, "\n".join(lines) + "\n"


def send_report_email(settings, subject, body):
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = settings.recipient
    message["Subject"] = subject
    message.set_content(body)
    cafile = ssl.get_default_verify_paths().cafile
    if sys.platform == "darwin" and cafile is None and Path("/etc/ssl/cert.pem").is_file():
        context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    else:
        context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.host, settings.port, timeout=15, context=context) as smtp:
        smtp.login(settings.username, settings.password)
        smtp.send_message(message)


def _boundary(day):
    return datetime.combine(day, REPORT_TIME, tzinfo=MOSCOW)


def _previous_boundary(end_local):
    if end_local.weekday() not in (0, 3):
        raise ValueError("Дата сводки должна быть понедельником или четвергом.")
    return end_local - timedelta(days=4 if end_local.weekday() == 0 else 3)


def _next_boundary(end_local):
    if end_local.weekday() not in (0, 3):
        raise ValueError("Дата сводки должна быть понедельником или четвергом.")
    return end_local + timedelta(days=3 if end_local.weekday() == 0 else 4)


def _last_due_boundary(now_local):
    for days_back in range(8):
        candidate = _boundary(now_local.date() - timedelta(days=days_back))
        if candidate.weekday() in (0, 3) and candidate <= now_local:
            return candidate
    raise ValueError("Не удалось определить дату сводки.")


def send_due_report(db, settings, now=None, send_email=send_report_email):
    """Отправляет один период между соседними пн/чт 10:00 МСК."""
    now_local = (now or datetime.now(timezone.utc)).astimezone(MOSCOW)
    latest_end = _last_due_boundary(now_local)
    last_sent = db.last_report_period_end()
    period_end = _next_boundary(_boundary(date.fromisoformat(last_sent))) if last_sent else latest_end
    if period_end > latest_end:
        return None
    subject, body = report_content(db, _previous_boundary(period_end), period_end)
    send_email(settings, subject, body)
    db.mark_report_sent(period_end.date().isoformat(), settings.recipient)
    return period_end.date()

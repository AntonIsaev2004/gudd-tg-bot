"""Сводки по понедельникам и четвергам и отправка через SMTP с TLS."""

import html
import re
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
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{5,32}")


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


def _local_db_time(value, *, short=False):
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    ).astimezone(MOSCOW).strftime("%d.%m %H:%M" if short else "%d.%m.%Y %H:%M")


def _one_line(value):
    return " ".join(str(value or "").split()) or "—"


def _count(value, singular, few, many):
    form = many if value % 100 in (11, 12, 13, 14) else (
        singular if value % 10 == 1 else few if value % 10 in (2, 3, 4) else many
    )
    return f"{value} {form}"


def _report_html(period, views_count, visitors_count, properties, visitors):
    property_rows = []
    for _item_id, title, views, people in properties:
        property_rows.append(
            '<tr>'
            f'<td style="padding:9px 6px;border-bottom:1px solid #e5e7eb;word-break:break-word;">{html.escape(title)}</td>'
            f'<td align="right" style="padding:9px 6px;border-bottom:1px solid #e5e7eb;">{views}</td>'
            f'<td align="right" style="padding:9px 6px;border-bottom:1px solid #e5e7eb;">{people}</td>'
            '</tr>'
        )

    visitors_by_property = {}
    for visitor in visitors:
        visitors_by_property.setdefault(visitor["property_id"], []).append(visitor)

    visitor_tables = []
    for item_id, title, _views, _people in properties:
        group = visitors_by_property.get(item_id)
        if not group:
            continue
        rows = []
        for visitor in group:
            username = visitor["username"]
            if username:
                label = html.escape(f"@{username}")
                nickname = (
                    f'<a href="https://t.me/{username}" '
                    f'style="color:#1d4ed8;text-decoration:underline;">{label}</a>'
                    if USERNAME_PATTERN.fullmatch(username) else label
                )
            else:
                nickname = "ник не указан"
            rows.append(
                '<tr>'
                '<th scope="row" align="left" style="padding:8px 5px;border-bottom:1px solid #e5e7eb;'
                'vertical-align:top;word-break:break-word;font-weight:normal;">'
                f'<strong>{html.escape(visitor["name"])}</strong><br>{nickname}</th>'
                '<td align="center" style="padding:8px 3px;border-bottom:1px solid #e5e7eb;vertical-align:top;">'
                f'{visitor["views"]}</td>'
                '<td style="padding:8px 5px;border-bottom:1px solid #e5e7eb;vertical-align:top;'
                'font-size:12px;white-space:nowrap;">'
                f'{html.escape(visitor["first_short"])}<br>{html.escape(visitor["last_short"])}</td>'
                '</tr>'
            )
        visitor_tables.append(
            f'<h3 style="margin:18px 0 7px;font-size:16px;">{html.escape(title)}</h3>'
            '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="width:100%;table-layout:fixed;border-collapse:collapse;font-size:13px;">'
            '<thead><tr style="background:#f3f4f6;">'
            '<th scope="col" align="left" width="42%" style="padding:8px 5px;">Посетитель</th>'
            '<th scope="col" align="center" width="16%" style="padding:8px 3px;">Просм.</th>'
            '<th scope="col" align="left" width="42%" style="padding:8px 5px;">Первый / последний</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
        )
    visitor_section = "".join(visitor_tables) if visitor_tables else '<p>За период просмотров не было.</p>'
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        '<body style="margin:0;background:#f9fafb;color:#111827;font-family:Arial,sans-serif;">'
        '<div style="max-width:600px;margin:0 auto;padding:16px;background:#ffffff;">'
        '<h1 style="margin:0 0 8px;font-size:22px;">Сводка GUDD</h1>'
        f'<p style="margin:0 0 16px;color:#4b5563;font-size:14px;">{html.escape(period)} (МСК)</p>'
        f'<p style="font-size:15px;">Просмотров: <strong>{views_count}</strong> · '
        f'Уникальных посетителей: <strong>{visitors_count}</strong></p>'
        '<h2 style="margin:22px 0 8px;font-size:18px;">Объекты</h2>'
        '<table width="100%" cellpadding="0" cellspacing="0" style="width:100%;table-layout:fixed;border-collapse:collapse;font-size:14px;">'
        '<thead><tr style="background:#f3f4f6;">'
        '<th scope="col" align="left" width="68%" style="padding:9px 6px;">Объект</th>'
        '<th scope="col" align="right" width="16%" style="padding:9px 6px;">Просм.</th>'
        '<th scope="col" align="right" width="16%" style="padding:9px 6px;">Люди</th>'
        '</tr></thead><tbody>'
        + "".join(property_rows) +
        '</tbody></table>'
        '<h2 style="margin:24px 0 8px;font-size:18px;">Посетители по объектам</h2>'
        + visitor_section +
        '</div></body></html>'
    )


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
    period = f"{start_local:%d.%m.%Y %H:%M}–{end_local:%d.%m.%Y %H:%M}"
    lines = [
        f"Сводка за {period} (МСК)",
        f"Просмотров объектов: {len(visits)}",
        f"Уникальных посетителей: {len(unique_visitors)}",
        "",
        "Объекты:",
    ]
    property_summaries = []
    for item_id, rows in by_property.items():
        visitor_count = len({row["telegram_id"] for row in rows})
        title = titles.get(item_id, "(объект удалён)")
        property_summaries.append((item_id, title, len(rows), visitor_count))
        lines.append(
            f"{title}: "
            f"{_count(len(rows), 'просмотр', 'просмотра', 'просмотров')}, "
            f"{_count(visitor_count, 'посетитель', 'посетителя', 'посетителей')}"
        )

    lines.extend(("", "Посетители по объектам:"))
    visitor_summaries = []
    for (item_id, _user_id), entry in by_visitor.items():
        row = entry["row"]
        full_name = " ".join(
            part for part in (_one_line(row["first_name"]), _one_line(row["last_name"])) if part != "—"
        ) or "Имя не указано"
        username = _one_line(row["username"]) if row["username"] else ""
        first = _local_db_time(entry["first"])
        last = _local_db_time(entry["last"])
        visitor_summaries.append({
            "property_id": item_id, "name": full_name, "username": username,
            "views": entry["count"], "first": first, "last": last,
            "first_short": _local_db_time(entry["first"], short=True),
            "last_short": _local_db_time(entry["last"], short=True),
        })
    if not visitor_summaries:
        lines.append("За период просмотров не было.")
    for item_id, title, _views, _people in property_summaries:
        group = [visitor for visitor in visitor_summaries if visitor["property_id"] == item_id]
        if group:
            lines.append(f"{title}:")
            for visitor in group:
                nickname = f"@{visitor['username']}" if visitor["username"] else "ник не указан"
                lines.append(
                    f"  {visitor['name']} · {nickname} · просмотров {visitor['views']} · "
                    f"первый {visitor['first']} · последний {visitor['last']}"
                )
    return subject, "\n".join(lines) + "\n", _report_html(
        period, len(visits), len(unique_visitors), property_summaries, visitor_summaries
    )


def send_report_email(settings, subject, body, html_body):
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = settings.recipient
    message["Subject"] = subject
    message.set_content(body)
    message.add_alternative(html_body, subtype="html")
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
    subject, body, html_body = report_content(db, _previous_boundary(period_end), period_end)
    send_email(settings, subject, body, html_body)
    db.mark_report_sent(period_end.date().isoformat(), settings.recipient)
    return period_end.date()

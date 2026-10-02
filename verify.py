"""Проверка локальной базы и тестовая отправка почтовой сводки."""

import argparse
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import load_setting
from reporting import MOSCOW, _last_due_boundary, load_report_settings, report_content, send_report_email
from storage import CatalogDB


ROOT = Path(__file__).resolve().parent


def database_path(env_file):
    configured = Path(load_setting(env_file, "DB_PATH") or "data/gudd.sqlite3")
    return configured if configured.is_absolute() else ROOT / configured


def check_database(path):
    if not path.is_file():
        raise RuntimeError(f"База не найдена: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        counts = {
            "objects": conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0],
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "visits": conn.execute(
                "SELECT COUNT(*) FROM user_events WHERE event_type = 'property_opened'"
            ).fetchone()[0],
        }
        recent = conn.execute(
            """SELECT property_id, created_at FROM user_events
               WHERE event_type = 'property_opened' ORDER BY id DESC LIMIT 5"""
        ).fetchall()
        delivery = conn.execute(
            "SELECT period_end, recipient, sent_at FROM report_deliveries ORDER BY period_end DESC LIMIT 1"
        ).fetchone()
    print(f"База: {path}")
    print(f"Целостность: {integrity}; ошибки связей: {len(foreign_keys)}")
    print(f"Объектов: {counts['objects']}; пользователей: {counts['users']}; просмотров: {counts['visits']}")
    for item_id, created_at in recent:
        print(f"  Просмотр объекта #{item_id}: {created_at} UTC")
    if delivery:
        print(f"Последнее письмо принято SMTP: период до {delivery[0]}, адресат {delivery[1]}, {delivery[2]} UTC")
    else:
        print("Успешные отправки пока не записаны.")
    if integrity != "ok" or foreign_keys:
        raise RuntimeError("В базе найдены ошибки целостности.")


def send_test_email(path, env_file):
    settings = load_report_settings(env_file, load_setting)
    if settings is None:
        raise RuntimeError("SMTP не настроен в .env.")
    now = datetime.now(timezone.utc).astimezone(MOSCOW)
    start = _last_due_boundary(now)
    end = now + timedelta(seconds=1)
    db = CatalogDB(path)
    try:
        subject, body, html_body = report_content(db, start, end)
    finally:
        db.close()
    send_report_email(
        settings, f"[ТЕСТ] {subject}",
        "Проверочное письмо; расписание не менялось.\n\n" + body,
        html_body,
    )
    print(f"Тестовая сводка за {start:%d.%m.%Y %H:%M}–{now:%d.%m.%Y %H:%M} МСК принята SMTP для {settings.recipient}.")
    print("Проверьте папки «Входящие» и «Спам» у адресата.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send-test-email", action="store_true", help="отправить текущую сводку без отметки о плановой отправке")
    args = parser.parse_args()
    env_file = ROOT / ".env"
    path = database_path(env_file)
    check_database(path)
    if args.send_test_email:
        send_test_email(path, env_file)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, sqlite3.Error) as exc:
        raise SystemExit(str(exc)) from None
    except (OSError, smtplib.SMTPException) as exc:
        raise SystemExit(f"Не удалось отправить письмо: {type(exc).__name__}. Проверьте сеть и настройки SMTP.") from None

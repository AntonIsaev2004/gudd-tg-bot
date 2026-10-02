"""Короткие подписи для кнопок Telegram."""


def compact_title(title: str, limit: int = 24) -> str:
    if len(title) <= limit:
        return title
    return title[:limit - 1].rstrip() + "…"

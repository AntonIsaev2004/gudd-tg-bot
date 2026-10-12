"""Общие правила команды очистки полей карточки."""

import re


OPTIONAL_TEXT_FIELDS = frozenset(("title", "teaser"))
SKIP_COMMAND = re.compile(r"/skip(?:@[A-Za-z0-9_]+)?")


def is_skip_command(value):
    return isinstance(value, str) and SKIP_COMMAND.fullmatch(value.strip()) is not None

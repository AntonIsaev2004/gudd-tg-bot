"""Общая модель карточки недвижимости."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Property:
    id: int
    title: str
    price: int
    area: float
    location: str
    rooms: str
    teaser: str
    description: str
    features: tuple
    photos: tuple

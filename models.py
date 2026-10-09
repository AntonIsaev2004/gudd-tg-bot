"""Общая модель карточки недвижимости."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Property:
    id: int
    title: str
    price: str
    area: float
    location: str
    rooms: str
    teaser: str
    description: str
    features: tuple
    photos: tuple
    sale_price: Optional[str] = None

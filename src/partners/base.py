from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import MenuResult, Restaurant


class Partner(ABC):
    name: str

    @abstractmethod
    async def find_menu(self, r: Restaurant) -> MenuResult | None:
        """Return the restaurant's menu, or None when this partner doesn't list it.

        Raise PartnerError for failures (blocked, malformed payload) so the caller can
        move on to the next source.
        """

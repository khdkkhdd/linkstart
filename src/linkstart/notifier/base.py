"""Notifier abstract interface."""
from abc import ABC, abstractmethod

from linkstart.models import Event


class Notifier(ABC):
    @abstractmethod
    async def notify(self, event: Event) -> None: ...

    async def close(self) -> None:
        return None

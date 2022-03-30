from abc import abstractmethod, ABC
from typing import Any
from .event_system import RayEvent

class EventPublisher(ABC):
    @abstractmethod
    def publish(self, trace_id: str, event: RayEvent):
        pass


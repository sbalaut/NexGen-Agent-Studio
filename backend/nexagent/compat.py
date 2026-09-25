"""Python 3.10 compatibility helpers."""
from enum import Enum

try:
    from enum import StrEnum  # Python 3.11+
except ImportError:  # pragma: no cover - exercised on 3.10 only
    class StrEnum(str, Enum):
        def __str__(self) -> str:
            return str(self.value)

        def __format__(self, spec: str) -> str:
            return format(str(self.value), spec)

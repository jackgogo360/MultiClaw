"""Circuit Breaker — 防止对故障服务器的无效重试"""

from __future__ import annotations

import time
from typing import Optional


_THRESHOLD = 3
_COOLDOWN_SEC = 60.0


class CircuitBreaker:
    """三态断路器：closed → open → half-open。

    连续失败达到阈值后进入 open 状态，冷却期后进入 half-open 允许一次探测。
    """

    def __init__(self, threshold: int = _THRESHOLD, cooldown: float = _COOLDOWN_SEC) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._failure_count = 0
        self._opened_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        if self._failure_count < self._threshold:
            return False
        if self._opened_at is None:
            return False
        elapsed = time.monotonic() - self._opened_at
        if elapsed >= self._cooldown:
            return False  # half-open
        return True

    @property
    def remaining_cooldown(self) -> float:
        if not self.is_open or self._opened_at is None:
            return 0.0
        elapsed = time.monotonic() - self._opened_at
        return max(0.0, self._cooldown - elapsed)

    def record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self._threshold and self._opened_at is None:
            self._opened_at = time.monotonic()

    def record_success(self) -> None:
        self._failure_count = 0
        self._opened_at = None

    def reset(self) -> None:
        self._failure_count = 0
        self._opened_at = None

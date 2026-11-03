"""标准 logging 封装: 保持旧版 `========== 分区 ==========` 的中文日志风格."""

from __future__ import annotations

import logging
import sys


class SectionHandler(logging.StreamHandler):
    """按 logger 名称输出分区标题, 同一分区连续输出时只打一次标题."""

    def __init__(self, stream=None) -> None:
        super().__init__(stream or sys.stdout)
        self._current: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name != self._current:
                self._current = record.name
                self.stream.write(f"========== {record.name} ==========\n")
                self.stream.flush()
            message = record.getMessage()
            if message:
                self.stream.write(message + "\n")
                self.stream.flush()
        except Exception:  # pragma: no cover - 标准库错误处理
            self.handleError(record)


def setup(level: int = logging.INFO) -> None:
    """初始化根 logger."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(SectionHandler())
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get(name: str) -> logging.Logger:
    """按分区名取 logger."""
    return logging.getLogger(name)


def die(message: str) -> None:
    """打印失败信息并以 1 退出 (与旧版行为一致)."""
    get("FATAL").error("[失败] %s", message)
    raise SystemExit(1)

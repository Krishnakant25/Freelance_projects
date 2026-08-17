"""Structured logging. JSON output for log aggregators (LOG_JSON=true),
human-readable otherwise."""
import json
import logging
import sys

from . import config

_STANDARD_ATTRS = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    if config.LOG_JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            HumanFormatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    _configured = True

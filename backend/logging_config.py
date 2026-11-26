import logging
import os
import re
import sys
import json
from datetime import datetime

# Secrets that can appear in request paths (MCP token-in-URL) and query strings.
_REDACT_PATTERNS = [
    (re.compile(r'([?&](?:token|v|_auth|x)=)[^&\s"]+'), r'\1***'),
    (re.compile(r'(/mcp/)([A-Za-z0-9_\-]{20,})'), r'\1***'),
]


def _redact(text: str) -> str:
    for pattern, repl in _REDACT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Strip MCP tokens from query strings and path segments in every log record.

    Attached once to the shared handler so it covers the uvicorn access log,
    the metrics middleware and the rate-limit warning with a single filter.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.getMessage())
        record.args = None
        extra = record.__dict__.get("extra_info")

        def _walk(value):
            if isinstance(value, str):
                return _redact(value)
            if isinstance(value, dict):
                return {k: _walk(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_walk(v) for v in value]
            return value

        if isinstance(extra, dict):
            record.extra_info = _walk(extra)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_record["exc_info"] = self.formatException(record.exc_info)
        if record.__dict__.get("extra_info"):
            log_record.update(record.extra_info)
        return json.dumps(log_record)

def setup_logging():
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    dev_mode = os.environ.get("DEV_MODE", "False").lower() == "true"

    logger = logging.getLogger()
    logger.setLevel(log_level)

    # Clear existing handlers to avoid duplicate logs in reloaded environments
    if logger.hasHandlers():
        logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    if dev_mode:
        formatter = logging.Formatter(
            "%(levelname)s:     %(name)s - %(message)s (%(filename)s:%(lineno)d)"
        )
    else:
        formatter = JsonFormatter()
    handler.setFormatter(formatter)
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)

    # Configure uvicorn access logger to use our handler
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.handlers.clear()
    uvicorn_access_logger.addHandler(handler)
    uvicorn_access_logger.propagate = False # Prevent logs from going to root logger again

    # Disable uvicorn default handler to prevent duplicate logging
    from uvicorn.config import LOGGING_CONFIG
    LOGGING_CONFIG["formatters"]["default"]["fmt"] = "%(message)s" # This won't be used if propagate is False

    # Example of how to get a logger in other modules
    # logger = logging.getLogger(__name__)

# Call setup_logging when this module is imported
setup_logging()

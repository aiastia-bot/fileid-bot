"""Redact credentials even when a third-party exception includes a request URL."""

import logging
import re


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        message = re.sub(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{15,}", "[BOT_TOKEN]", message)
        return re.sub(r"(\w+://)[^\s/@]+:[^\s/@]+@", r"\1[CREDENTIALS]@", message)

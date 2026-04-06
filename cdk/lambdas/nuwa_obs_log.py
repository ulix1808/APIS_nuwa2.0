"""Trazas INFO para CloudWatch: entrada de handler y esperas en recursos externos."""

from __future__ import annotations

import logging
from typing import Any, Mapping

_LOGGER = logging.getLogger("nuwa.obs")
_LOGGER.setLevel(logging.INFO)


def log_handler_enter(name: str, event: Mapping[str, Any], context: Any) -> None:
    rid = getattr(context, "aws_request_id", "") if context is not None else ""
    method = str(event.get("httpMethod") or "?").upper()
    path = str(event.get("path") or "")[:400]
    _LOGGER.info(
        "handler_enter name=%s request_id=%s %s %s",
        name,
        rid,
        method,
        path,
    )


def log_phase(phase: str, detail: str = "") -> None:
    _LOGGER.info("phase=%s %s", phase, detail)


def log_await(service: str, action: str, target: str = "") -> None:
    _LOGGER.info("await service=%s action=%s target=%s", service, action, target)


def log_done(service: str, action: str, detail: str = "") -> None:
    _LOGGER.info("done service=%s action=%s %s", service, action, detail)

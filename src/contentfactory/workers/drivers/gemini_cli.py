"""Gemini CLI (`gemini`) — chỉ dùng chung hợp đồng discovery/probe/classify ở W1."""
from __future__ import annotations

import re

from contentfactory.workers.errors import WorkerErrorClass
from .base import BaseDriver


class GeminiCliDriver(BaseDriver):
    id = "gemini_cli"
    label = "Google Gemini CLI"
    exe_names = ("gemini",)
    extra_patterns = (
        (WorkerErrorClass.QUOTA, re.compile(r"exceeded your current quota|quota exceeded", re.I)),
        (WorkerErrorClass.AUTH, re.compile(r"run /auth|credentials (?:are )?not (?:found|available)", re.I)),
    )

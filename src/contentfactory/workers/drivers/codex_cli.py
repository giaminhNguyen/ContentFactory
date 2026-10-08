"""Codex CLI (`codex`) — chỉ dùng chung hợp đồng discovery/probe/classify ở W1."""
from __future__ import annotations

import re

from contentfactory.workers.errors import WorkerErrorClass
from .base import BaseDriver


class CodexCliDriver(BaseDriver):
    id = "codex_cli"
    label = "OpenAI Codex CLI"
    exe_names = ("codex",)
    extra_patterns = (
        (WorkerErrorClass.QUOTA, re.compile(r"usage limit reached|exceeded.*quota", re.I)),
        (WorkerErrorClass.AUTH, re.compile(r"not logged in|please log in|chatgpt session expired", re.I)),
    )

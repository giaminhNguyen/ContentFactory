"""Claude Code CLI (`claude`) — driver đầu tiên của W1.

Phần khác biệt của CLI này (cú pháp stream-json, resume session, kiểm auth) nằm trong file này.
`execute`/`build_command` cho story.write được nối vào ở C6 khi pipeline chạy qua WorkerManager.
"""
from __future__ import annotations

import re

from contentfactory.workers.errors import WorkerErrorClass
from .base import BaseDriver


class ClaudeCliDriver(BaseDriver):
    id = "claude_cli"
    label = "Claude Code CLI"
    exe_names = ("claude",)
    extra_patterns = (
        (WorkerErrorClass.QUOTA, re.compile(r"claude (?:usage|rate) limit|credit balance is too low", re.I)),
        (WorkerErrorClass.AUTH, re.compile(r"please run /login|claude (?:is )?not (?:yet )?authenticated", re.I)),
    )

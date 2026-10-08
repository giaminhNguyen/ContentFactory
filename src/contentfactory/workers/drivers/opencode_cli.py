"""opencode CLI (`opencode`) — chỉ dùng chung hợp đồng discovery/probe/classify ở W1."""
from __future__ import annotations

import re

from contentfactory.workers.errors import WorkerErrorClass
from .base import BaseDriver


class OpencodeCliDriver(BaseDriver):
    id = "opencode_cli"
    label = "opencode CLI"
    exe_names = ("opencode",)
    extra_patterns = (
        (WorkerErrorClass.QUOTA, re.compile(r"rate limit|too many requests", re.I)),
        (WorkerErrorClass.AUTH, re.compile(r"api key (?:is )?(?:missing|invalid)|not authenticated", re.I)),
    )

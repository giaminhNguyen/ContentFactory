"""TemplateClient: ContentFactory's door to ContentFlow's template system (D-92). ContentFlow OWNS templates and assets; this only
asks it (`python -m templating <command>`, JSON on stdin/stdout, one process per call, no import of ContentFlow code).

Every method returns the plain `result` data or raises StageError (POLICY for a template/asset problem the user can fix, RESOURCE
when ContentFlow cannot be started). Method names = ContentFlow `templating.Service` methods:
  list_templates, get_template, versions, validate, create_draft, duplicate, new_draft, save_draft, publish, archive, delete_draft, delete_template,
  resolve, resolve_many, preview, test_render, list_assets, get_asset, validate_asset, asset_path, import_asset, delete_asset, info
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ..contracts import ErrorClass, StageError

METHODS = frozenset({"list_templates", "get_template", "versions", "validate", "create_draft", "duplicate", "new_draft", "save_draft", "publish",
                     "archive", "delete_draft", "delete_template", "resolve", "resolve_many", "preview", "test_render", "list_assets", "get_asset", "validate_asset",
                     "asset_path", "import_asset", "delete_asset", "info", "migrate_legacy"})


class TemplateClient:
    def __init__(self, root: Path, python: str | None = None, user_root: Path | None = None, timeout_s: float = 600.0) -> None:
        self.root = Path(root)
        self.python = python or sys.executable
        self.user_root = Path(user_root) if user_root else None
        self.timeout_s = timeout_s

    def _env(self) -> dict:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        if self.user_root:
            env["CONTENTFLOW_USER_ROOT"] = str(self.user_root)
        return env

    def call(self, command: str, **payload):
        try:
            p = subprocess.run([self.python, "-m", "templating", command], cwd=str(self.root), input=json.dumps(payload, ensure_ascii=True),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout_s, env=self._env())
        except (OSError, subprocess.SubprocessError) as e:
            where = f"thư mục ContentFlow {self.root} không tồn tại" if not self.root.is_dir() else f"không chạy được {self.python!r}"
            raise StageError(ErrorClass.RESOURCE, "CONTENTFLOW_MISSING", f"không gọi được hệ thống template của ContentFlow: {where}: {e}",
                             resource="runtime") from None
        lines = [ln for ln in (p.stdout or "").splitlines() if ln.startswith("{")]
        if not lines:
            tail = (p.stderr or "")[-400:]
            raise StageError(ErrorClass.RESOURCE, "TEMPLATES_UNAVAILABLE",
                             f"ContentFlow không có hệ thống template (python -m templating, rc={p.returncode}); cập nhật module ContentFlow: {tail}",
                             resource="runtime")
        try:
            out = json.loads(lines[-1])
        except ValueError:
            raise StageError(ErrorClass.TRANSIENT, "TEMPLATES_BAD_REPLY", f"trả lời không đọc được: {lines[-1][:200]}") from None
        if out.get("ok"):
            return out.get("result")
        err = out.get("error") or {}
        raise StageError(ErrorClass.POLICY, str(err.get("code") or "TEMPLATE_ERROR"), str(err.get("message") or "lỗi template"),
                         dict(err.get("details") or {}), resource="input")

    def __getattr__(self, name: str):
        if name in METHODS:
            return lambda **kw: self.call(name.replace("_", "-"), **kw)
        raise AttributeError(name)

"""Test frontend: logic thuần bằng `node --test`, cú pháp mọi module JS, và khớp giữa hợp đồng backend <-> bảng trạng thái của giao diện."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from contentfactory.orchestrator import diagnose as DG
from contentfactory.orchestrator.service import FILTERS

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "src" / "contentfactory" / "orchestrator" / "webui_static"
NODE = shutil.which("node")


def node(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([NODE, *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout, cwd=str(REPO))


@unittest.skipUnless(NODE, "cần Node.js")
class FrontendTest(unittest.TestCase):
    def test_pure_logic_tests_pass(self):
        r = node("--test", "tests/ui_js/logic.test.mjs", "tests/ui_js/templates.test.mjs", "tests/ui_js/story_guidance.test.mjs", "tests/ui_js/rerun.test.mjs")
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-1000:])

    def test_every_module_parses(self):
        files = sorted(STATIC.rglob("*.js"))
        self.assertGreaterEqual(len(files), 15)
        for f in files:
            if "vendor" in f.parts:
                continue
            r = node("--check", str(f))
            self.assertEqual(r.returncode, 0, f"{f.name}: {r.stderr[:500]}")

    def test_views_exported_for_every_route(self):
        router = (STATIC / "js" / "router.js").read_text(encoding="utf-8")
        for name in ("run", "jobs", "job", "batch", "channels", "tts", "pools", "settings", "templates", "studio"):
            self.assertIn(f'"{name}"', router)
            src = (STATIC / "js" / "views" / f"{name}.js").read_text(encoding="utf-8")
            self.assertIn("export async function mount", src, name)
            self.assertIn("destroy", src, f"{name}: view phải dọn tài nguyên khi đóng")

    def test_no_view_assigns_html_from_data(self):
        bad = [f.name for f in STATIC.rglob("*.js") if "vendor" not in f.parts and any(x in f.read_text(encoding="utf-8") for x in (".innerHTML =", "insertAdjacentHTML", "document.write"))]
        # icons.js dùng innerHTML với hằng số SVG của chính nó (không có dữ liệu ngoài)
        self.assertEqual([b for b in bad if b != "icons.js"], [])

    def test_status_tables_match_the_backend_contract(self):
        r = node("--input-type=module", "-e", f"import * as s from 'file:///{(STATIC / 'js' / 'status.js').as_posix()}'; console.log(JSON.stringify({{job:Object.keys(s.JOB_STATUS),stage:Object.keys(s.STAGE_STATE),filters:s.FILTERS.map(f=>f[0])}}))")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        backend_job = {"running", "queued", "waiting", "attention", "completed", "failed", "paused", "cancelled"}
        self.assertEqual(set(d["job"]), backend_job)
        for case in ({"state": "NEW"}, {"state": "TTS_RUNNING"}, {"state": "FAILED"}, {"state": "PUBLISHED"}, {"state": "SOURCE_READY", "hold_reason": "PAUSED_QUOTA"},
                     {"state": "SOURCE_READY", "hold_reason": "PAUSED_CREDENTIAL"}, {"state": "TTS_RUNNING", "control_state": "PAUSED"},
                     {"state": "SOURCE_READY", "control_state": "CANCELLED"}):
            self.assertIn(DG.ui_status({"hold_reason": None, "needs_user": False, "target_idx": 7, **case}), d["job"])
        self.assertEqual(set(d["stage"]), {"done", "reused", "provided", "running", "waiting", "held", "failed", "not_planned"})
        self.assertEqual(d["filters"], list(FILTERS))
        r2 = node("--input-type=module", "-e", f"import * as s from 'file:///{(STATIC / 'js' / 'status.js').as_posix()}'; console.log(JSON.stringify(Object.keys(s.TIMELINE)))")
        from contentfactory.orchestrator.service import TIMELINE_LABEL
        self.assertEqual(set(json.loads(r2.stdout)), set(TIMELINE_LABEL))                  # nhãn timeline của giao diện = tập trạng thái backend trả

    def test_index_html_is_self_contained_and_accessible(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('lang="vi"', html)
        self.assertIn("skip-link", html)
        self.assertIn("<!--CF_TOKEN-->", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))        # không phụ thuộc CDN/mạng ngoài
        self.assertNotIn("https://", html)
        css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn(":focus-visible", css)
        self.assertIn('prefers-color-scheme: dark', css)


if __name__ == "__main__":
    unittest.main()

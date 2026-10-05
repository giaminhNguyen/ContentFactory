import contextlib
import io
import json
import shutil
import unittest

from contentfactory.jobs import pipeline as P
from contentfactory.fsutil import sha256_file
from contentfactory.orchestrator.cli import main as cli_main
from contentfactory.story.validate import validate_story_text
from tests.support import RootCase, params


class E2ETest(RootCase):
    def test_job_runs_through_all_fake_stages(self):
        orc = self.orc()
        jid = orc.submit(params())
        self.assertEqual(orc.store.get_job(jid)["state"], P.NEW)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)

        # mỗi stage chạy đúng 1 lần, state đi đúng thứ tự
        self.assertEqual(self.runs(orc, jid), {s.name: ["succeeded"] for s in P.STAGES})
        seq = [t["to_state"] for t in orc.store.transitions(jid)]
        expected = [P.NEW] + [x for s in P.STAGES for x in (s.running_state, s.done_state)]
        self.assertEqual(seq, expected)

        # manifest: đủ stage, artifact khớp file trên đĩa
        m = json.loads((self.job_dir(jid) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((m["state"], m["schema"]), (P.PUBLISHED, 1))
        self.assertEqual(set(m["stages"]), {s.name for s in P.STAGES})
        self.assertIn("ContentFlow", m["modules"])
        for st in m["stages"].values():
            self.assertEqual(st["status"], "succeeded")
            self.assertTrue(st["stage_key"])
            for a in st["artifacts"]:
                self.assertEqual(sha256_file(self.job_dir(jid) / a["path"]), a["sha256"])

    def test_output_package_matches_handoff_layout(self):
        orc = self.orc()
        jid = orc.submit(params())
        orc.run()
        [proj] = [d for d in (self.root / "output").iterdir()]
        self.assertRegex(proj.name, r"^\d{8}_truyen-ma-dem-khuya$")          # D-07: ngày + slug ASCII
        files = {p.relative_to(proj).as_posix() for p in proj.rglob("*") if p.is_file()}
        self.assertTrue({"README.txt", "project.json", "story.txt", "youtube/video.mp4", "youtube/thumbnail.jpg",
                         "youtube/title.txt", "youtube/description.txt", "tiktok/part_01.mp4"} <= files)
        m = json.loads((self.job_dir(jid) / "manifest.json").read_text(encoding="utf-8"))
        n_parts = len(m["stages"]["render_tiktok"]["artifacts"])
        self.assertGreaterEqual(n_parts, 2)
        self.assertEqual(sum(f.startswith("tiktok/") for f in files), n_parts)
        self.assertEqual(validate_story_text((proj / "story.txt").read_text(encoding="utf-8")), [])
        self.assertFalse([f for f in files if f.endswith((".part", ".log", ".json")) and f != "project.json"])
        self.assertFalse((proj.parent / f".tmp-{jid}").exists())

    def test_output_is_not_an_internal_dependency(self):
        orc = self.orc()
        jid = orc.submit(params())
        orc.run()
        shutil.rmtree(self.root / "output")                     # người dùng xóa output: pipeline không quan tâm
        orc2 = self.orc()
        orc2.run()
        j = orc2.store.get_job(jid)
        self.assertEqual(j["state"], P.PUBLISHED)
        self.assertEqual(self.runs(orc2, jid), {s.name: ["succeeded"] for s in P.STAGES})

    def test_jobs_have_isolated_workspaces(self):
        orc = self.orc()
        a, b = orc.submit(params()), orc.submit(params())
        orc.run()
        self.assertNotEqual(self.job_dir(a), self.job_dir(b))
        for jid in (a, b):
            for art in orc.store.artifacts(jid):
                self.assertTrue((self.job_dir(jid) / art["path"]).is_file())
        names = sorted(d.name for d in (self.root / "output").iterdir())
        self.assertEqual(len(names), 2)                          # cùng tiêu đề: không ghi đè, hậu tố -2
        self.assertTrue(names[1].endswith("-2"))

    def test_structured_logs(self):
        orc = self.orc()
        jid = orc.submit(params())
        orc.run()
        lines = [json.loads(x) for x in (self.job_dir(jid) / "job.log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all({"ts", "level", "event", "job_id"} <= set(r) for r in lines))
        started = [r["stage"] for r in lines if r["event"] == "stage_started"]
        done = [r["stage"] for r in lines if r["event"] == "stage_succeeded"]
        self.assertEqual(started, [s.name for s in P.STAGES])
        self.assertEqual(done, started)
        glob = (self.root / "runtime" / "logs" / "orchestrator.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertTrue(all(json.loads(x) for x in glob))

    def test_cli_submit_run_status(self):
        base = ["--root", str(self.root)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli_main(base + ["submit", "--input", "https://youtu.be/x", "--set", "made_for_kids=false",
                             "--set", 'tiktok={"speed":2.0,"target_part_sec":1.5}'])
            jid = out.getvalue().strip()
            cli_main(base + ["run"])
            cli_main(base + ["status", jid])
        self.assertIn(f"job {jid}: PUBLISHED", out.getvalue())


if __name__ == "__main__":
    unittest.main()

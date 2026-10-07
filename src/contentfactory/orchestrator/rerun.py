"""Selective Manual Rerun (D-113): người dùng chọn CHÍNH XÁC các stage muốn chạy lại cho một job (kể cả job đã hoàn tất/đã đăng).

Ba mảnh, một nguồn sự thật (không logic nghiệp vụ nào nằm ở JS):
  1. PHÂN TÍCH (`analyze`): kết quả của mỗi stage đã chạy còn đồng bộ (`fresh`) hay không (`stale`). Dùng đúng cơ chế sẵn có — khóa của lần chạy được lưu tách phần
     (`meta.lineage` = {rest: băm tham số/config, inputs: {kind: [sha256]}}); so với dữ liệu hiện hành để biết ĐẦU VÀO NÀO hoặc tham số nào đã đổi, rồi lan theo từng KIND artifact
     (`Stage.sources_of`): kind chỉ stale khi một kind nó suy ra từ đã đổi/stale hoặc tham số của stage đổi. Chạy lại Truyện ra sha mới ⇒ TTS/Audio/video/gói/đăng stale; nhánh
     không liên quan (hoặc nội dung y hệt) thì không. Không có cờ lưu sẵn, không có hệ thống "stale" thứ hai. Lần chạy cũ không có lineage rơi về so khóa nguyên khối.
  2. KẾ HOẠCH (`check`/`expand`): với tập stage đã chọn (sắp theo thứ tự pipeline, KHÔNG tự thêm bước giữa, KHÔNG chạy từ bước đầu tới cuối), mỗi stage cần input hiện hành và đồng bộ:
     input do stage CHỌN TRƯỚC nó sinh ra thì hợp lệ; ngược lại input phải còn file, và kind đó không được stale hoặc sắp bị làm stale bởi một stage được chọn phía trước
     (kết quả cũ lệch với upstream mới) — khi đó từ chối và gợi ý “Hãy chọn thêm …”. Backend quyết định, UI chỉ hiển thị.
  3. THỰC THI (`execute`): một phiên chạy nền, tuần tự theo thứ tự pipeline, KHÔNG đụng máy trạng thái của job (job PUBLISHED vẫn PUBLISHED). Mỗi stage được resolve input MỚI
     NHẤT lúc bắt đầu (stage sau dùng artifact do stage trước sinh ra trong cùng phiên), chạy trong thư mục RIÊNG (`<workdir>/r<seq>`) nên cache/sidecar cũ không phát lại,
     và có định danh thực thi `sha256(stage_key | phiên)` thay cho stage_key làm idempotency (publish: phiên mới => upload mới; retry/resume trong cùng phiên => cùng upload).
     Artifact chỉ được thay ở `commit_stage` (một transaction): lỗi/hủy giữa chừng giữ nguyên kết quả hiện hành.

Retry KHÁC rerun: retry là lỗi TRANSIENT của cùng một execution (cùng định danh, không tăng `rerun_count`); rerun là execution mới do người dùng yêu cầu (tăng đếm).
"""
from __future__ import annotations

import hashlib
import json
import random
import time

from ..contracts import ErrorClass, JobCancelToken, StageContext, StageError
from ..jobs import pipeline as P
from ..jobs.db import CONTROL_CANCELLED, CONTROL_DELETED, CONTROL_PAUSED
from ..jobs.policy import outcome_for
from ..jobs.workspace import ensure_job_dirs, job_dir
from ..story import guidance as GD
from .diagnose import STAGE_LABEL
from .handlers import HANDLERS
from .revisions import depends_on
from .stages import StageContract

PRODUCER = {k: s.name for s in P.STAGES for k in s.produces}
DEPS = {s.name: sorted(depends_on(s), key=P.INDEX.__getitem__) for s in P.STAGES}


def _err(code: str, message: str, hint: str = "", **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, message, {**detail, **({"hint": hint} if hint else {})}, resource="input")


def _labels(names) -> str:
    return ", ".join(STAGE_LABEL[n] for n in sorted(names, key=P.INDEX.__getitem__))


def exec_key(stage_key: str, session_id: str, stage: str) -> str:
    """Định danh thực thi của MỘT stage trong MỘT phiên chạy lại: ổn định khi retry/crash-resume cùng phiên, khác mọi phiên khác và khác stage_key ngữ nghĩa."""
    return hashlib.sha256(f"{stage_key}|manual_rerun|{session_id}|{stage}".encode()).hexdigest()


def kind_ancestors(kind: str) -> set[str]:
    """Các stage mà `kind` thực sự suy ra từ (theo `Stage.sources_of`, đệ quy)."""
    out: set[str] = set()
    seen: set[str] = set()
    todo = [kind]
    while todo:
        k = todo.pop()
        prod = PRODUCER.get(k)
        if prod is None or k in seen:
            continue
        seen.add(k)
        for src in P.BY_NAME[prod].sources_of(k):
            q = PRODUCER.get(src)
            if q:
                out.add(q)
                todo.append(src)
    return out


KIND_ANCESTORS = {k: kind_ancestors(k) for k in PRODUCER}


class RerunService:
    def __init__(self, orc) -> None:
        self.orc = orc
        self.store = orc.store

    # ============================================================================================ phân tích
    @staticmethod
    def _file_ok(jd, a: dict) -> bool:
        p = jd / a["path"]
        try:
            return p.is_file() and p.stat().st_size == a["bytes"]
        except OSError:
            return False

    def analyze(self, job: dict) -> dict:
        """{"stages": {tên: {state: fresh|provided|absent, stale, stale_by, files_ok, last}}, "kind_stale": {kind: bool}}. `stale` = đã chạy nhưng kết quả không còn đồng bộ
        với đầu vào/tham số hiện hành. Chỉ đọc DB + hash khóa, không băm file lớn."""
        jid = job["id"]
        jd = job_dir(self.orc.cfg.path("workspace"), jid)
        runs, arts = self.store.stage_runs(jid), self.store.artifacts(jid)
        last: dict[str, dict] = {}
        for r in runs:
            if r["status"] == "succeeded" or (r["status"] == "skipped" and json.loads(r["data"] or "{}").get("skipped") == "valid"):
                last[r["stage"]] = r
        imported = {a["kind"] for a in arts if a["stage"] == "import"}
        stages: dict[str, dict] = {}
        kind_stale: dict[str, bool] = {}
        for s in P.STAGES:                                                                    # theo thứ tự pipeline: nguồn của một kind luôn được tính trước
            rows = [a for a in arts if a["stage"] == s.name]
            row = last.get(s.name)
            info = {"stage": s.name, "state": "absent", "stale": False, "stale_by": [], "files_ok": True, "last": row}
            if row and rows:
                contract = StageContract(s)
                inputs, _ = contract.scope_inputs(self.store.inputs(jid, s.requires + s.optional), job.get("pipeline"))
                ready, _ = contract.can_run({k for k, v in inputs.items() if v})
                extra = GD.run_extra(row)
                parts = contract.key_parts(job["params"], job.get("config_snapshot"), inputs, extra)
                old = json.loads(row["meta"] or "{}").get("lineage")
                if old is not None:
                    rest_changed = parts["rest"] != old["rest"]
                    changed = {k for k in set(parts["inputs"]) | set(old["inputs"]) if parts["inputs"].get(k) != old["inputs"].get(k)}
                else:                                                                         # lần chạy cũ chưa ghi lineage: chỉ biết khóa nguyên khối lệch hay không
                    key = contract.stage_key(job["params"], job.get("config_snapshot"), inputs, extra) if ready else None
                    rest_changed = key != row["stage_key"]
                    changed = set(parts["inputs"]) if rest_changed else set()
                by: set[str] = set()
                for kind in s.produces:
                    src = s.sources_of(kind)
                    drift = [k for k in src if k in changed or kind_stale.get(k)]
                    kind_stale[kind] = rest_changed or bool(drift)
                    by |= {PRODUCER[k] for k in drift if k in PRODUCER and PRODUCER[k] != s.name}
                info.update(state="fresh", stale=any(kind_stale[k] for k in s.produces), stale_by=sorted(by, key=P.INDEX.__getitem__),
                            files_ok=all(self._file_ok(jd, a) for a in rows))
            elif set(s.produces) & imported:
                info.update(state="provided", files_ok=all(self._file_ok(jd, a) for a in arts if a["stage"] == "import" and a["kind"] in s.produces))
            stages[s.name] = info
        return {"stages": stages, "kind_stale": kind_stale}

    @staticmethod
    def _input_kinds(job: dict, store, s: P.Stage) -> list[tuple[str, bool]]:
        """(kind, bắt buộc) mà stage `s` thực sự dùng với dữ liệu hiện có: requires + nhánh đóng gói đang dùng + input tùy chọn đã có."""
        inputs = store.inputs(job["id"], s.requires + s.optional)
        scoped, pkg = StageContract(s).scope_inputs(inputs, job.get("pipeline"))
        req = list(s.requires) + [k for k in pkg if k not in s.requires]
        return [(k, True) for k in req] + [(k, False) for k in s.optional if scoped.get(k) and k not in req]

    # ============================================================================================ kế hoạch
    def blocker(self, job: dict) -> dict | None:
        """Lý do CẢ JOB không chạy lại được lúc này (chặn mọi stage)."""
        if job["control_state"] == CONTROL_DELETED:
            return {"code": "JOB_NOT_FOUND", "message": "Job đã bị xóa.", "hint": "Quay lại danh sách job."}
        if job["control_state"] == CONTROL_CANCELLED:
            return {"code": "JOB_CANCELLED", "message": "Job đã bị hủy nên không chạy lại được.", "hint": "Dùng “Chạy lại với thay đổi” để tạo job mới từ phần còn hợp lệ."}
        if job["state"] in P.BY_RUNNING or job.get("lease_owner"):
            return {"code": "JOB_BUSY", "message": "Job đang chạy một bước.", "hint": "Chờ bước hiện tại xong hoặc tạm dừng job rồi chạy lại."}
        if self.store.reruns.active(job["id"]):
            return {"code": "RERUN_BUSY", "message": "Job đang có một lượt chạy lại chưa xong.", "hint": "Chờ lượt đó kết thúc rồi chọn lại."}
        if job["control_state"] == CONTROL_PAUSED:
            return {"code": "JOB_PAUSED", "message": "Job đang tạm dừng.", "hint": "Bấm Tiếp tục job (hoặc Chạy tiếp) trước khi chạy lại bước."}
        return None

    def _problems(self, job: dict, an: dict, name: str, before: set[str]) -> list[dict]:
        """Vấn đề để chạy stage `name` khi các stage trong `before` (được chọn, đứng trước) sẽ chạy xong trước nó. Mỗi vấn đề:
        {code, message, hint, needs: [stage cần thêm], hard}. `hard` = không có stage nào sửa được."""
        s = P.BY_NAME[name]
        probs: list[dict] = []
        p = job["params"]
        if name == "source" and not str((p.get("input") or {}).get("value") or "").strip():
            probs.append({"code": "NO_SOURCE_INPUT", "message": "Job này không có nguồn (link/phụ đề) để lấy lại phụ đề.", "hint": "Dùng job khởi tạo từ link hoặc file phụ đề.", "needs": [], "hard": True})
        if name == "publish":
            chp = ((job.get("config_snapshot") or {}).get("semantic", {}).get("channel_config") or {}).get("publishing") or {}
            if not isinstance(p.get("made_for_kids", chp.get("made_for_kids")), bool):
                probs.append({"code": "MISSING_MADE_FOR_KIDS", "message": "Chưa khai báo video có dành cho trẻ em hay không.", "hint": "Đặt made_for_kids cho job hoặc kênh.", "needs": [], "hard": True})
        arts = self.store.artifacts(job["id"])
        jd = job_dir(self.orc.cfg.path("workspace"), job["id"])
        stages = an["stages"]
        for kind, required in self._input_kinds(job, self.store, s):
            prod = PRODUCER[kind]
            if prod in before:
                continue                                                                      # sẽ được sinh ra trong chính phiên này, trước stage này
            rows = [a for a in arts if a["kind"] == kind]
            if not rows:
                if required:
                    probs.append({"code": "INPUT_MISSING", "message": f"Thiếu {kind}: {STAGE_LABEL[prod]} chưa có kết quả.",
                                  "hint": f"Hãy chọn thêm {STAGE_LABEL[prod]}.", "needs": [prod], "hard": False})
                continue
            from_import = all(a["stage"] == "import" for a in rows)
            if not all(self._file_ok(jd, a) for a in rows):
                probs.append({"code": "ARTIFACT_MISSING", "message": f"File {kind} không còn trên đĩa (đã dọn hoặc bị xóa).",
                              "hint": f"Hãy chọn thêm {STAGE_LABEL[prod]} để tạo lại.", "needs": [prod], "hard": False})
                continue
            if from_import:
                continue                                                                      # dữ liệu do người dùng đưa vào: không có gì "cũ" hơn
            ups = before & KIND_ANCESTORS[kind]
            if an["kind_stale"].get(kind):
                by = stages[prod]["stale_by"]
                probs.append({"code": "INPUT_STALE", "message": f"Không thể chạy lại {STAGE_LABEL[name]} vì {STAGE_LABEL[prod]} hiện không còn đồng bộ" + (f" với {_labels(by)}." if by else "."),
                              "hint": f"Hãy chọn thêm {STAGE_LABEL[prod]}.", "needs": [prod], "hard": False})
            elif ups:
                probs.append({"code": "INPUT_STALE", "message": f"Không thể chạy lại {STAGE_LABEL[name]} vì {STAGE_LABEL[prod]} sẽ không còn đồng bộ sau khi chạy lại {_labels(ups)}.",
                              "hint": f"Hãy chọn thêm {STAGE_LABEL[prod]}.", "needs": [prod], "hard": False})
        return probs

    def check(self, job: dict, stages, an: dict | None = None) -> dict:
        """Kiểm tra một lựa chọn NGUYÊN VĂN (không tự thêm bước). Trả {order, stages:[{id,label,problems}], ok}."""
        an = an if an is not None else self.analyze(job)
        order = sorted(set(stages), key=P.INDEX.__getitem__)
        rows = [{"id": n, "label": STAGE_LABEL[n], "problems": self._problems(job, an, n, set(order[:i]))} for i, n in enumerate(order)]
        return {"order": order, "stages": rows, "ok": bool(order) and not any(r["problems"] for r in rows)}

    def expand(self, job: dict, stages, an: dict | None = None) -> tuple[list[str], dict]:
        """Tập nhỏ nhất chứa lựa chọn mà các vấn đề sửa được đã được sửa (thêm stage sinh input còn thiếu/stale). Trả (thứ tự, kết quả check của tập đó)."""
        an = an if an is not None else self.analyze(job)
        cur = set(stages)
        for _ in range(len(P.STAGES) + 1):
            res = self.check(job, cur, an)
            add = {n for r in res["stages"] for p in r["problems"] if not p["hard"] for n in p["needs"]} - cur
            if not add:
                return res["order"], res
            cur |= add
        return res["order"], res

    # ============================================================================================ API
    def _job(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if job is None or job["control_state"] == CONTROL_DELETED:
            raise _err("JOB_NOT_FOUND", f"Không có job {job_id}.", "Quay lại danh sách job.")
        return job

    def options(self, job_id: str) -> dict:
        job = self._job(job_id)
        blocked = self.blocker(job)
        an = self.analyze(job)
        counts = self.store.reruns.counts(job_id)
        rows = []
        for s in P.STAGES:
            info = an["stages"][s.name]
            order, res = self.expand(job, {s.name}, an)
            probs = [p for r in res["stages"] for p in r["problems"]]
            hard = next((p for p in probs if p["hard"]), None)
            needs = [n for n in order if n != s.name]
            alone = self._problems(job, an, s.name, set())
            first = alone[0] if alone else None
            top = blocked or hard or first
            reason = top["code"] if top else None
            hint = (top["hint"] if blocked or hard else (first["message"] + (f" Hãy chọn thêm: {_labels(needs)}." if needs else ""))) if top else None
            rows.append({"id": s.name, "label": STAGE_LABEL[s.name], "eligible": not blocked and hard is None, "ready_alone": not blocked and not alone,
                         "reason": reason, "hint": hint, "needs": needs, "rerun_count": counts.get(s.name, 0), "stale": info["stale"], "state": info["state"],
                         "stale_by": info["stale_by"], "depends_on": DEPS[s.name], "ran_at": (info["last"] or {}).get("ended_at")})
        story = self.orc.stage_extra("story", job["params"])["story_guidance"]
        return {"job_id": job_id, "blocked": blocked, "stages": rows, "active": self.view_active(job_id),
                "story_guidance": {"source": story["source"], "text": story["text"]}}

    def plan(self, job_id: str, stages) -> dict:
        job = self._job(job_id)
        sel = self.normalize(stages)
        blocked = self.blocker(job)
        an = self.analyze(job)
        res = self.check(job, sel, an)
        order, _ = self.expand(job, sel, an)
        return {"job_id": job_id, "ok": res["ok"] and not blocked, "blocked": blocked, "order": res["order"], "stages": res["stages"],
                "suggested": [n for n in order if n not in sel], "publishes": "publish" in sel}

    @staticmethod
    def normalize(stages) -> list[str]:
        if not isinstance(stages, (list, tuple)) or not stages:
            raise _err("EMPTY_SELECTION", "Chưa chọn bước nào để chạy lại.", "Tích ít nhất một bước.")
        bad = sorted({str(x) for x in stages if not isinstance(x, str) or x not in P.INDEX})
        if bad:
            raise _err("INVALID_STAGE", f"Bước không hợp lệ: {bad}.", f"Hợp lệ: {[s.name for s in P.STAGES]}", valid=[s.name for s in P.STAGES])
        return sorted(set(stages), key=P.INDEX.__getitem__)              # gộp trùng, theo thứ tự pipeline

    def start(self, job_id: str, stages, request_id: str | None = None) -> dict:
        sel = self.normalize(stages)
        job = self._job(job_id)
        if request_id:
            old = self.store.reruns.by_request(job_id, request_id)
            if old is not None:                                                            # bấm đúp / hai tab cùng request: một phiên
                return {**self.view(old), "deduped": True}
        blocked = self.blocker(job)
        if blocked:
            raise _err(blocked["code"], blocked["message"], blocked["hint"])
        res = self.check(job, sel)
        if not res["ok"]:
            order, _ = self.expand(job, sel)
            probs = [p for r in res["stages"] for p in r["problems"]]
            need = [n for n in order if n not in sel]
            raise _err("RERUN_NOT_ELIGIBLE", probs[0]["message"], (f"Hãy chọn thêm: {_labels(need)}." if need and not any(p["hard"] for p in probs) else probs[0]["hint"]),
                       stages=res["stages"], suggested=need)
        sess, created = self.store.reruns.create(job_id, sel, request_id)
        if created:
            self.orc.log.emit("rerun_requested", job_id=job_id, session=sess["id"], number=sess["number"], stages=sel)
            self.orc._manifest(job_id)
        return {**self.view(sess), "deduped": not created}

    # -- hiển thị ---------------------------------------------------------------------------------------
    def view(self, s: dict, runs: list[dict] | None = None) -> dict:
        runs = [r for r in (runs if runs is not None else self.store.stage_runs(s["job_id"])) if r["rerun_session_id"] == s["id"]]
        job = self.store.get_job(s["job_id"]) or {}
        cp = (job.get("checkpoint") or {})
        stages = []
        for n in s["requested"]:
            info = s["stages"].get(n, {})
            mine = [r for r in runs if r["stage"] == n]
            last = mine[-1] if mine else None
            meta = json.loads(last["meta"] or "{}") if last else {}
            data = json.loads(last["data"] or "{}") if last and last["data"] else {}
            row = {"id": n, "label": STAGE_LABEL[n], "state": info.get("state", "pending"), "attempts": len(mine), "started_at": mine[0]["started_at"] if mine else None,
                   "ended_at": info.get("ended_at"), "error": info.get("error")}
            if info.get("state") == "running":
                c = cp.get(n) or {}
                row["progress"] = {"done": c.get("done"), "total": c.get("total"), "detail": c.get("detail")}
            if n == "story" and meta.get("guidance"):
                g = meta["guidance"]
                row["guidance"] = {"source": g.get("source"), "text": g.get("text"), "hash": g.get("hash"), "resolved_at": g.get("resolved_at")}
            if n == "publish" and data.get("remote_id"):
                row["remote"] = {"id": data.get("remote_id"), "url": data.get("remote_url")}
            stages.append(row)
        done = [r["state"] for r in stages]
        result = s["state"] if s["state"] in ("queued", "running", "cancelled") else ("succeeded" if s["state"] == "succeeded" else
                 "partial" if "succeeded" in done else "failed")
        return {"id": s["id"], "number": s["number"], "job_id": s["job_id"], "state": s["state"], "result": result, "requested": s["requested"], "stages": stages,
                "created_at": s["created_at"], "started_at": s["started_at"], "ended_at": s["ended_at"], "error": s["error"], "trigger": s["trigger"]}

    def view_active(self, job_id: str) -> dict | None:
        s = self.store.reruns.active(job_id)
        return self.view(s) if s else None

    def history(self, job_id: str) -> dict:
        self._job(job_id)
        runs = self.store.stage_runs(job_id)
        initial = []
        for s in P.STAGES:
            mine = [r for r in runs if r["stage"] == s.name and not r["rerun_session_id"]]
            if not mine:
                continue
            last = mine[-1]
            meta = json.loads(last["meta"] or "{}")
            data = json.loads(last["data"] or "{}") if last["data"] else {}
            row = {"id": s.name, "label": STAGE_LABEL[s.name], "status": last["status"], "attempts": len(mine), "started_at": mine[0]["started_at"], "ended_at": last["ended_at"]}
            if s.name == "story" and meta.get("guidance"):
                row["guidance"] = {k: meta["guidance"].get(k) for k in ("source", "text", "hash", "resolved_at")}
            if s.name == "publish" and data.get("remote_id"):
                row["remote"] = {"id": data.get("remote_id"), "url": data.get("remote_url")}
            initial.append(row)
        return {"job_id": job_id, "initial": initial, "sessions": [self.view(s, runs) for s in self.store.reruns.sessions(job_id)],
                "counts": self.store.reruns.counts(job_id)}

    # ============================================================================================ thực thi
    def execute(self, sid: str) -> None:
        """Chạy một phiên (gọi từ executor của orchestrator). Idempotent khi được nhận lại sau crash: stage đã `succeeded` được bỏ qua."""
        orc = self.orc
        sess = self.store.reruns.get(sid)
        if sess is None:
            return
        job_id = sess["job_id"]
        token = JobCancelToken(orc.cancel, lambda: self.store.job_signal(job_id))
        orc._tokens[job_id] = token
        try:
            for n in sess["requested"]:
                if self.store.reruns.get(sid)["stages"].get(n, {}).get("state") == "succeeded":
                    continue
                self.store.reruns.set_stage(sid, n, state="running")
                err = self._run_stage(sid, job_id, n, token)
                if err is None:
                    continue
                if err.error_class == ErrorClass.CANCELLED:
                    reason = token.reason or "shutdown"
                    if reason == "abort":
                        self.store.reruns.set_stage(sid, n, state="cancelled", ended_at=time.time())
                        self._skip_rest(sid, [x for x in sess["requested"] if P.INDEX[x] > P.INDEX[n]], "Đã hủy.")
                        self.store.reruns.finish(sid, "cancelled")
                    else:
                        self.store.reruns.set_stage(sid, n, state="pending")
                        self.store.reruns.requeue(sid)                                     # Tạm dừng/tắt app: giữ phiên, chạy tiếp khi sẵn sàng (cùng định danh thực thi)
                    return
                self.store.reruns.set_stage(sid, n, state="failed", ended_at=time.time(), error=err.to_dict())
                self._skip_rest(sid, [x for x in sess["requested"] if P.INDEX[x] > P.INDEX[n]], f"Bỏ qua vì {STAGE_LABEL[n]} lỗi.")
                self.store.reruns.finish(sid, "failed", err.to_dict())
                orc.log.emit("rerun_failed", "error", job_id, session=sid, stage=n, error=err.to_dict())
                return
            self.store.reruns.finish(sid, "succeeded")
            orc.log.emit("rerun_succeeded", job_id=job_id, session=sid)
        except Exception as e:                                                             # noqa: BLE001 — một lỗi bất ngờ không được làm kẹt phiên ở trạng thái running
            orc.log.emit("rerun_exception", "error", job_id, session=sid, error=repr(e))
            self.store.reruns.finish(sid, "failed", StageError(ErrorClass.POLICY, "UNEXPECTED", repr(e)).to_dict())
        finally:
            orc._tokens.pop(job_id, None)
            orc._manifest(job_id)

    def _skip_rest(self, sid: str, names, why: str) -> None:
        for x in names:
            if self.store.reruns.get(sid)["stages"].get(x, {}).get("state") in ("pending", None):
                self.store.reruns.set_stage(sid, x, state="skipped", error={"message": why})

    def _run_stage(self, sid: str, job_id: str, name: str, token: JobCancelToken) -> StageError | None:
        orc, store = self.orc, self.store
        stage = P.BY_NAME[name]
        contract = StageContract(stage)
        used, rand = 0, random.random
        while True:
            job = store.get_job(job_id)
            sess = store.reruns.get(sid)
            if job is None or job["control_state"] in (CONTROL_DELETED, CONTROL_CANCELLED):
                return StageError(ErrorClass.CANCELLED, "CANCELLED", "job đã bị hủy/xóa")
            jd = ensure_job_dirs(orc.cfg.path("workspace"), job_id)
            policy = orc._policy(job.get("config_snapshot"))
            run_id = None
            try:
                token.check()
                an = self.analyze(job)
                done_before = {x for x in sess["requested"] if P.INDEX[x] < P.INDEX[name] and sess["stages"].get(x, {}).get("state") == "succeeded"}
                probs = self._problems(job, an, name, done_before)                      # kiểm LẠI trên dữ liệu thật lúc này (có thể đã đổi từ lúc yêu cầu)
                if probs:
                    raise StageError(ErrorClass.POLICY, "RERUN_NOT_ELIGIBLE", probs[0]["message"], {"problems": probs, "hint": probs[0]["hint"]}, resource="input")
                inputs, package_kinds = contract.scope_inputs(store.inputs(job_id, stage.requires + stage.optional), job.get("pipeline"))
                contract.validate_inputs(inputs, jd, also=package_kinds)
                extra = {**orc.stage_extra(name, job["params"]), "rerun_dir": f"r{sess['seq']:04d}"}    # Story: đề xuất hiệu lực TẠI THỜI ĐIỂM chạy lại (inherit => Cài đặt mới nhất)
                key = contract.stage_key(job["params"], job.get("config_snapshot"), inputs, GD.key_extra(extra.get("story_guidance")))
                while True:                                                             # chờ chỗ trên tài nguyên dùng chung (vd 1 GPU)
                    token.check()
                    got = store.reruns.begin_run(sid, job_id, stage, orc.owner, orc.cfg.limit(P.resource_of(stage)))
                    if got is not None:
                        break
                    token.wait(0.5)
                run_id, attempt = got
                ek = exec_key(key, sid, name)
                store.set_run_key(run_id, key, orc.run_meta(contract, job["params"], job.get("config_snapshot"), inputs, extra, True,
                                                            trigger="manual_rerun", rerun_session=sid, rerun_number=sess["number"], exec_key=ek[:16]))
                store.reruns.set_stage(sid, name, state="running", attempts=attempt, run_id=run_id, started_at=time.time())
                log = orc.log.bind(job_id, name, attempt)
                stage_dir = jd / stage.workdir / f"r{sess['seq']:04d}"
                stage_dir.mkdir(parents=True, exist_ok=True)
                config = orc.stage_config(job.get("config_snapshot"))
                if name == "tts":
                    config["tts_cache_dir"] = str(stage_dir / "_cache")                 # không phát lại cache chunk dùng chung: synth lại thật
                ctx = StageContext(job_id=job_id, stage=name, attempt=attempt, stage_key=ek, workspace=jd, stage_dir=stage_dir, params=job["params"],
                                   inputs=inputs, config=config, cancel=token, log=log, progress=orc._progress_fn(job_id, name), extra=extra)
                orc.monitor.preflight(stage)
                log("stage_started", stage_key=key[:12], trigger="manual_rerun", session=sid)
                t0 = time.time()
                adapters = {**orc._adapters_for(job.get("config_snapshot")), "sequence": orc.sequence}
                result = HANDLERS[name](ctx, **{n: adapters[n] for n in stage.adapters})
                arts = orc._seal(stage, result, jd, contract)
                data = {**result.data, "trigger": "manual_rerun", "rerun_session": sid}
                if not store.reruns.commit_stage(job_id, stage, run_id, arts, data):
                    store.reruns.end_run(run_id, "cancelled")
                    return StageError(ErrorClass.CANCELLED, "CANCELLED", "job đã bị hủy/xóa trước khi lưu kết quả")
                store.reruns.set_stage(sid, name, state="succeeded", ended_at=time.time(), error=None)
                log("stage_succeeded", seconds=round(time.time() - t0, 3), artifacts=len(arts), data=data)
                return None
            except StageError as e:
                if run_id:
                    store.reruns.end_run(run_id, "cancelled" if e.error_class == ErrorClass.CANCELLED else "failed", None if e.error_class == ErrorClass.CANCELLED else e.to_dict())
                if e.error_class == ErrorClass.CANCELLED:
                    return e
                out = outcome_for(e, used, policy, time.time(), rand)
                if out.action == "retry":                                               # retry của CÙNG execution: cùng phiên, cùng định danh, không tăng rerun_count
                    used += 1
                    orc.log.emit("rerun_stage_retry", "warning", job_id, session=sid, stage=name, error=e.code, delay=round(out.delay, 2))
                    try:
                        token.wait(out.delay)
                    except StageError as c:
                        return c
                    continue
                return e
            except Exception as e:                                                      # noqa: BLE001
                if run_id:
                    store.reruns.end_run(run_id, "failed", StageError(ErrorClass.POLICY, "UNEXPECTED", repr(e)).to_dict())
                orc.log.emit("stage_exception", "error", job_id, stage=name, error=repr(e))
                return StageError(ErrorClass.POLICY, "UNEXPECTED", repr(e))

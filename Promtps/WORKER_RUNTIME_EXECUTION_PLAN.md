# ContentFactory Worker Runtime — Phase Execution Plan

> **Muc dich:** day la file giao viec cho coding agent. Agent chay mot phase nao thi chi duoc dung khi **toan bo chuc nang, test, migration, tai lieu va UI/UX cua phase do da hoan tat**.
>
> **Ten phase:** dung `W1`, `W2`, `W3` de tranh nham voi cac Phase 1–10 hien co cua ContentFactory.

---

## 0. Execution Contract — bat buoc doc truoc khi lam

### 0.1 Cach chay

Agent nhan mot bien muc tieu:

```text
TARGET_PHASE=W1
```

hoac:

```text
TARGET_PHASE=W2
TARGET_PHASE=W3
```

Quy tac:

1. Doc `README.md`, `HANDOFF.md`, `docs/CURRENT_SYSTEM_AUDIT.md`, `docs/DECISIONS.md`, `docs/MODULE_CONTRACTS.md`, `docs/UI_GUIDE.md` neu ton tai.
2. Khong pha vo nguyen tac hien tai: **ContentFactory la orchestrator duy nhat va DB ContentFactory la source of truth**.
3. Neu phase muc tieu phu thuoc phase truoc ma prerequisite chua dat, **sua prerequisite truoc**, sau do moi tiep tuc phase muc tieu.
4. Khong duoc hardcode pipeline theo ten `claude`, `codex`, `gemini`, `opencode` hay vendor khac.
5. Ten agent/model van duoc hien thi cho nguoi dung. Core scheduler chi lam viec voi `worker_id`, `driver_id`, `model_id`, pool, policy va trang thai.
6. Khong duoc coi subprocess exit code `0` la thanh cong neu output chua qua validation.
7. Moi output cua worker phai o attempt workspace rieng; chi promote sang canonical artifact sau khi validation pass.
8. Khong duoc retry/fallback vo han.
9. Khong duoc su dung fallback de lach quota/provider enforcement.
10. Neu mot acceptance gate khong pass, phase **khong duoc danh dau COMPLETE**.

### 0.2 UI/UX contract — bat buoc cho moi phase co UI

Truoc khi sua UI, agent **bat buoc doc va ap dung hai skill da cai trong project**:

- `ui-ux-pro-max-skill`
- `gsap-skills`

Neu skill nam trong thu muc rieng cua project, tim va doc file huong dan cua skill truoc khi code.

UI khong duoc chi la "dua du lieu ra man hinh". UI phai la san pham hoan chinh:

- visual hierarchy ro rang;
- spacing, typography, iconography, status language nhat quan;
- khong tao admin-table tho neu card/list/detail phu hop hon;
- khong neon/gradient/animation vo muc dich;
- animation phai giai thich quan he hoac thay doi trang thai;
- support `prefers-reduced-motion`;
- keyboard focus ro rang;
- responsive o kich thuoc cua so desktop nho va man hinh lon;
- text dai khong vo layout;
- khong de horizontal overflow ngoai y muon;
- cac state bat buoc: `loading`, `empty`, `normal`, `error`, `degraded`, `disabled` khi co y nghia;
- destructive action phai co guardrail;
- action co the undo thi uu tien Undo thay vi popup hoi lien tuc;
- moi entity duoc them vao san pham phai xem xet lifecycle: create/add, view, edit, enable/disable, test/validate, remove/delete, dependency impact. Chi bo mot action neu co ly do san pham ro rang.

GSAP chi dung cho motion co gia tri:

- drawer/panel transitions;
- worker/pool add-remove;
- reorder priority bang Flip/Draggable neu stack hien tai phu hop;
- status transition nhe;
- routing preview transition.

Bat buoc cleanup animation theo lifecycle framework hien tai, tranh memory leak va duplicate timeline.

### 0.3 Definition of Done chung

Phase chi COMPLETE khi:

- [ ] Tat ca muc `BUG/GAP -> FIX` cua phase da xu ly.
- [ ] Migration/schema thay doi co backward-safe path hoac migration ro rang.
- [ ] Unit tests moi pass.
- [ ] Integration tests lien quan pass.
- [ ] Existing tests khong regression.
- [ ] UI state tests/e2e tests lien quan pass.
- [ ] UI duoc visual QA o state binh thuong + rong + loi + degraded.
- [ ] Keyboard/focus co the dung duoc.
- [ ] Reduced-motion khong bi vo interaction.
- [ ] Khong de TODO/placeholder cho chuc nang nam trong scope phase.
- [ ] `doctor`/diagnostic output duoc cap nhat neu phase thay doi runtime dependency.
- [ ] Docs va config example duoc cap nhat.
- [ ] Co mot acceptance report ngan o cuoi task: da lam gi, test nao pass, con blocker nao.

Neu co blocker external that su (vi du provider khong cho dang nhap trong moi truong test), agent phai:

- hoan thanh deterministic fake/mock tests;
- ghi ro external verification nao chua the chay;
- **khong** tuyen bo gate production-external da pass neu chua co bang chung.

---

# W0 — Current Baseline / Problems to Remove

> W0 khong phai phase implementation doc lap. Day la danh sach gap ma W1 phai xu ly.

## W0.1 Pipeline con biet implementation cu the

**BUG/GAP**

Story hien tai gan truc tiep voi `StoryBranchAdapter`/Claude Code CLI. Neu thay Claude bang worker khac, pipeline co nguy co phai sua.

**FIX TARGET**

Pipeline chi yeu cau mot `WorkType`, vi du:

```text
story.write
```

va giao cho WorkerManager.

---

## W0.2 Chua co contract Worker/Driver chung

**BUG/GAP**

Moi agent CLI co cach detect, auth, model, command, output va error khac nhau.

**FIX TARGET**

Chuan hoa qua `Driver` contract; instance thuc te la `Worker`.

---

## W0.3 Quota/auth/timeout cua mot agent co the anh huong ca job

**BUG/GAP**

Tai nguyen cua mot worker bi loi khong nen dong nghia pipeline phai dung ngay.

**FIX TARGET**

Loi worker duoc classify va routing/fallback theo policy. Chi hold khi khong con worker hop le.

---

## W0.4 Chua co Task -> Attempt history cho worker runtime

**BUG/GAP**

Kho debug fallback, retry, invalid output, process crash.

**FIX TARGET**

Mot task co nhieu attempt; moi attempt immutable ve ket qua va worker/model da dung.

---

## W0.5 UI chua co control surface cho worker runtime

**BUG/GAP**

Nguoi dung chua co mot noi dep, de hieu de scan, them, sua, test worker; tao/sua pool; cau hinh fallback/model; xem tai sao worker duoc chon.

**FIX TARGET**

W1 bo sung Worker Control UI hoan chinh, khong phai debug page.

---

# W1 — Core Worker Runtime + Polished Product UI

> **Muc tieu W1:** ContentFactory khong con phu thuoc ten agent trong pipeline. Nguoi dung co the scan/cau hinh worker va model, tao pool, dat priority/retry/fallback, va he thong co the tu chuyen worker khi worker loi/quota/auth/timeout theo policy.

> **W1 la phase bat buoc truoc khi Worker Runtime duoc xem la dung duoc.**

---

## W1.1 Domain model: Work, Worker, Driver, Pool, Attempt

### BUG/GAP

Khong co abstraction runtime chung; logic de bi vendor-specific.

### FIX

Them cac domain toi thieu:

```text
WorkType
Worker
Driver
WorkerPool
Attempt
ExecutionTarget = Worker + Model
```

Khuyen nghi module:

```text
src/contentfactory/workers/
  models.py
  manager.py
  registry.py
  router.py
  attempts.py
  errors.py
  drivers/
    base.py
    claude_cli.py
    codex_cli.py
    gemini_cli.py
    opencode_cli.py
```

Khong bat buoc dung chinh xac ten file neu repo co convention khac, nhung separation of responsibility phai tuong duong.

### ACCEPTANCE

- [ ] Pipeline layer khong co conditional vendor-specific nhu `if claude...`.
- [ ] Story work co the goi WorkerManager bang `work_type`/contract chung.
- [ ] Core test dung fake driver van chay ma khong cai bat ky CLI that nao.

---

## W1.2 Driver contract

### BUG/GAP

Moi CLI co giao dien khac nhau.

### FIX

Driver contract toi thieu phai cover:

```text
discover()
probe()
list_or_validate_models()
build/execute headless command
normalize result
classify common errors
```

Driver duoc phep biet vendor. WorkerManager khong duoc biet syntax CLI cu the.

### ACCEPTANCE

- [ ] Co fake/test driver.
- [ ] It nhat 3 driver thuc te co implementation contract (neu CLI co san trong project/may).
- [ ] Driver khong log secret/token.
- [ ] Version probe failure khong crash app.

---

## W1.3 Worker Discovery

### BUG/GAP

Manager chua biet may dang co agent nao.

### FIX

Discovery W1 chi can:

```text
PATH
configured executable path
--version / safe identity probe
basic auth/status probe neu CLI ho tro an toan
```

Khong crawl filesystem rong; khong goi prompt ton token chi de health check.

Trang thai toi thieu:

```text
DETECTED
READY
AUTH_REQUIRED
BROKEN
DISABLED
NOT_FOUND
```

### ACCEPTANCE

- [ ] `Scan workers` tim duoc CLI tren PATH.
- [ ] Cho phep add manual executable path.
- [ ] Duplicate detection khong tao worker trung vo ly.
- [ ] CLI bi go bo -> worker thanh unavailable, khong bi xoa lich su.
- [ ] Rescan co the phat hien CLI moi ma khong can restart app neu architecture UI cho phep.

---

## W1.4 Worker configuration lifecycle

### BUG/GAP

Neu chi co "Add Worker" thi chua la feature hoan chinh.

### FIX

Worker W1 phai co:

```text
Add/Register
View
Edit display name/basic settings
Enable
Disable
Test/Probe
Remove from ContentFactory
```

`Remove from ContentFactory` khong dong nghia uninstall CLI tren OS.

Neu worker dang nam trong pool, remove phai:

- bao dependency;
- yeu cau remove/replace reference;
- khong de dangling config.

### ACCEPTANCE

- [ ] Add worker.
- [ ] Edit worker.
- [ ] Enable/disable.
- [ ] Test worker.
- [ ] Remove an toan.
- [ ] Dependency guard hoat dong.
- [ ] Khong mat attempt/history cu khi disable.

---

## W1.5 Model configuration

### BUG/GAP

Mot worker co the co nhieu model; pipeline khong nen hardcode model vendor.

### FIX

W1 support:

```text
models available/known
allowed models
default model
simple profile mapping: fast / balanced / high
```

Khong can Model Registry phuc tap rieng.

Driver chiu trach nhiem validate/translate model sang CLI invocation.

### ACCEPTANCE

- [ ] User xem duoc model config cua worker.
- [ ] Set default model.
- [ ] Enable/disable model neu phu hop.
- [ ] Invalid model bi phat hien truoc execution neu driver co the validate.
- [ ] Work co the yeu cau profile thay vi vendor model ID.

---

## W1.6 Worker Pool

### BUG/GAP

Pipeline can mot nhom worker thay vi mot vendor cu the.

### FIX

Pool W1 support:

```text
Create
View
Rename/Edit
Add worker
Remove worker
Reorder priority
Duplicate
Delete with dependency check
Enable/disable neu can
```

Routing strategies W1 chi can:

```text
priority
least_busy
```

Khong lam adaptive/AI scoring o W1.

### ACCEPTANCE

- [ ] Pool co lifecycle day du nhu tren.
- [ ] Reorder persist dung.
- [ ] Pool rong khong duoc route task va co thong bao ro.
- [ ] Xoa pool dang duoc WorkType su dung bi chan hoac co replace flow.

---

## W1.7 Work routing config

### BUG/GAP

WorkType chua biet dung pool/policy/model profile nao theo config.

### FIX

Cau hinh toi thieu:

```text
work_type -> pool
work_type -> model_profile
work_type -> retry/fallback policy
```

Vi du logical config:

```yaml
story.write:
  pool: story_workers
  model_profile: high
```

Config phai editable qua UI; YAML/JSON chi la representation.

### ACCEPTANCE

- [ ] Doi pool cua `story.write` khong sua code.
- [ ] Doi priority worker khong sua pipeline.
- [ ] Doi model/profile khong sua pipeline.

---

## W1.8 Error normalization

### BUG/GAP

CLI tra error khac nhau; fallback se khong dang tin neu logic dua vao raw string scattered.

### FIX

W1 chuan hoa it nhat:

```text
TEMPORARY
QUOTA
AUTH
TIMEOUT
INVALID_OUTPUT
UNKNOWN
```

Khong can taxonomy 20+ loai o W1.

### ACCEPTANCE

- [ ] Moi driver map duoc raw error pho bien ve common error.
- [ ] Unknown error co bounded retry; khong loop vo han.
- [ ] Error co structured metadata de hien UI.

---

## W1.9 Retry + Fallback policy

### BUG/GAP

Fallback khong duoc chi la list `Claude -> Gemini -> Codex`.

### FIX

Policy W1:

```text
TEMPORARY      -> retry same worker N lan, sau do next eligible
QUOTA          -> block worker/resource tai thoi diem do, next eligible
AUTH           -> mark auth-required, next eligible
TIMEOUT        -> retry same worker mot so lan nho, sau do next eligible
INVALID_OUTPUT -> retry/repair bounded, sau do next eligible
UNKNOWN        -> bounded retry/fallback
```

Limits toi thieu:

```text
max_total_attempts
max_distinct_workers
```

### ACCEPTANCE

Fault injection test:

- [ ] Worker A TEMPORARY -> retry A -> success.
- [ ] Worker A QUOTA -> Worker B duoc chon.
- [ ] Worker A AUTH -> A bi loai khoi candidate -> B chay.
- [ ] Worker A TIMEOUT -> bounded retry -> B.
- [ ] A+B fail -> C co the tiep quan neu con eligible.
- [ ] Tat ca unavailable -> task hold/fail ro rang, khong loop.

---

## W1.10 Simple cooldown

### BUG/GAP

Worker loi lap lai co the bi moi task goi lai ngay.

### FIX

W1 chi can simple cooldown:

```text
failure_streak
cooldown_until
```

Vi du 3 failure lien tiep -> cooldown 5 phut (configurable).

Khong can full sliding-window circuit breaker o W1.

### ACCEPTANCE

- [ ] Worker dang cooldown khong duoc router chon.
- [ ] Het cooldown co the duoc probe/eligible lai.

---

## W1.11 Task/Attempt persistence

### BUG/GAP

Khong co audit ro tai sao fallback.

### FIX

Moi work task ghi lich su attempt toi thieu:

```text
attempt_id
task/job/stage/work_type
worker_id
model_id
state
start/end
exit/error class
workspace
validation result
```

### ACCEPTANCE

- [ ] UI co the reconstruct attempt timeline.
- [ ] Retry/fallback tao attempt moi, khong overwrite attempt cu.
- [ ] Existing pipeline checkpoint van la source of truth cua stage.

---

## W1.12 Isolated attempt workspace + atomic promote

### BUG/GAP

Worker cu va worker moi khong duoc cung ghi canonical output.

### FIX

Moi attempt ghi vao workspace rieng:

```text
workspace/<job>/worker_attempts/<attempt_id>/
```

Output chi promote khi:

```text
process complete
+ output exists
+ schema/contract valid
+ deterministic validation pass
```

### ACCEPTANCE

- [ ] Kill worker giua ghi file khong corrupt canonical artifact.
- [ ] Attempt fail de lai evidence/debug nhung khong promote output loi.
- [ ] Chi current successful attempt duoc commit.

---

## W1.13 Context handoff toi thieu

### BUG/GAP

Worker B khong the tiep quan neu task phu thuoc session cua Worker A.

### FIX

Story work phai co canonical input/handoff toi thieu gom:

```text
objective/instructions
input transcript/canon can thiet
completed canonical artifacts
checkpoint
continuity/metadata can thiet
```

Khong truyen raw terminal log lam context mac dinh.

Partial output chua commit khong la canonical.

### ACCEPTANCE

- [ ] Worker B co the chay lai tu safe checkpoint ma khong can session A.
- [ ] Test fake worker A fail giua section; worker B tiep quan tu checkpoint cuoi da commit.

---

## W1.14 Output validation gate

### BUG/GAP

Exit code `0` chua du de thanh cong.

### FIX

Them validation layer truoc commit. Với Story, toi thieu kiem:

```text
file exists
encoding/readable
non-empty/min constraints
khong marker ky thuat bi cam
contract-specific checks hien co
```

Tai su dung validator san co cua ContentFactory neu da ton tai.

### ACCEPTANCE

- [ ] Worker exit 0 + output invalid -> task khong success.
- [ ] Invalid output kich hoat policy dung.
- [ ] Valid output promote dung.

---

# W1 UI — bat buoc, khong phai stretch goal

## W1.UI.1 Information architecture

W1 chi can 3 khu vuc chinh de tranh over-engineering:

```text
Workers
Pools
Routing & Reliability
```

Co the nam trong Settings/Control Center hien tai thay vi them sidebar qua nhieu muc.

Khong tao Workflow Node Editor o W1.

---

## W1.UI.2 Workers screen

### BUG/GAP

UI danh sach tho se kho dung va khong the hien health/action tot.

### FIX

Thiet ke worker surface co visual hierarchy ro:

Moi worker phai the hien toi thieu:

```text
display name
agent/driver label
health/status
current/default model
capacity/running neu co
pool membership ngan gon
primary action / overflow menu
```

Action:

```text
Open
Edit
Test
Enable/Disable
Remove
```

Top-level:

```text
Scan Workers
Add Manually
```

### STATE COVERAGE

- [ ] Loading scan.
- [ ] Empty: chua co worker -> CTA scan/add.
- [ ] Normal.
- [ ] AUTH_REQUIRED.
- [ ] QUOTA/temporarily unavailable neu co.
- [ ] Disabled.
- [ ] Broken/CLI missing.

### VISUAL QUALITY GATE

- [ ] Khong phai mot HTML table mac dinh.
- [ ] Card/list density phu hop desktop app, khong qua to.
- [ ] Status color khong la cach duy nhat truyen dat y nghia.
- [ ] Long names/path khong vo layout.
- [ ] Overflow action de hieu.

---

## W1.UI.3 Add/Edit Worker drawer or focused panel

### FIX

Flow toi thieu:

```text
Choose detected/manual
Name
Executable/driver
Probe result
Model config
Basic execution settings
Save
```

Khong can wizard 5 trang neu mot drawer/panel giai quyet gon hon.

### ACCEPTANCE

- [ ] Add detected worker.
- [ ] Add manual path.
- [ ] Edit existing worker.
- [ ] Validation inline, khong chi toast chung chung.
- [ ] Save button state ro.
- [ ] Close co warning neu co unsaved changes.

---

## W1.UI.4 Pools screen

### FIX

Pool editor phai cho user hieu ngay thu tu worker.

Bat buoc:

```text
Create pool
Rename/Edit
Add/remove worker
Reorder priority
Duplicate
Delete
Strategy: Priority / Least Busy
```

Dung GSAP Flip/Draggable **neu phu hop voi stack**, hoac motion tuong duong, cho reorder. Khong de DOM snap giat cuc.

### ACCEPTANCE

- [ ] Reorder bang mouse.
- [ ] Reorder co keyboard alternative hoac buttons accessible.
- [ ] Undo/revert immediate action neu implementation ho tro an toan.
- [ ] Delete co dependency impact.
- [ ] Empty pool state ro rang.

---

## W1.UI.5 Routing & Reliability screen

### FIX

Khong lam visual policy graph o W1. Dung mot panel dep, de doc:

Vi du:

```text
Story Write
Pool              Story Workers
Model profile     High

Temporary error   Retry 2x, then next worker
Quota             Next worker
Auth              Disable for routing, next worker
Timeout           Retry 1x, then next worker
Invalid output    Retry 1x, then next worker

Max attempts      6
```

### ACCEPTANCE

- [ ] User sua duoc pool/model profile/retry/fallback ma khong code.
- [ ] Values co safe defaults.
- [ ] Advanced fields khong overwhelm main form.
- [ ] Invalid combination bi chan truoc save.

---

## W1.UI.6 Routing Simulator

### WHY KEEP

Gia tri debug rat cao, implementation tuong doi nho.

### FIX

Cho user chon mot WorkType va bam `Simulate`.

Ket qua vi du:

```text
Claude Main   READY    selected
Gemini Main   READY    backup #1
Codex         DISABLED excluded
```

Phai noi ro **tai sao** worker bi exclude.

Khong goi model that; chi chay routing logic.

### ACCEPTANCE

- [ ] Simulation khong ton token.
- [ ] Ket qua khop voi router thuc.
- [ ] Co empty/no-eligible state.

---

## W1.UI.7 Motion + accessibility gate

- [ ] Dung 2 skill da chi dinh de review UI implementation.
- [ ] Drawer/card/reorder motion co chu dich.
- [ ] `prefers-reduced-motion` hoat dong.
- [ ] Focus order hop ly.
- [ ] Escape dong drawer/modal khi phu hop.
- [ ] Buttons/icon-only co accessible label.
- [ ] Contrast du dung.
- [ ] UI dung duoc o desktop viewport nho ma khong vo layout.

---

## W1.15 Doctor / CLI diagnostics

### FIX

Them command hoac mo rong Doctor:

```text
workers scan/list/probe
```

Hoac mapping vao CLI convention hien tai.

Output toi thieu:

```text
Worker
Driver
Executable
Version
Auth/ready state
Default model
Status
```

### ACCEPTANCE

- [ ] Doctor khong can UI.
- [ ] Loi 1 worker khong lam doctor crash.
- [ ] Output human-readable.

---

## W1.16 Compliance-safe behavior

### BUG/GAP

Automation co the bi dung sai de ne quota/provider enforcement.

### FIX

W1 rule:

- quota worker -> block worker/resource, route worker hop le khac;
- khong rotate account tu dong de ne provider limit;
- khong scrape/steal OAuth token tu tool khac;
- uu tien documented headless/CLI/API surfaces;
- secret khong luu vao log/config plain text neu CLI tu quan credential.

### ACCEPTANCE

- [ ] No automatic account-rotation logic.
- [ ] Quota test route sang worker khac, khong tim credential khac cua cung provider de bypass.

---

## W1 Final Acceptance Scenario — bat buoc

Agent phai tao automated integration scenario (fake drivers duoc chap nhan) va neu co CLI that san sang thi chay smoke test that:

```text
Story Work created
-> Worker A selected
-> A quota failure
-> A becomes unavailable for routing
-> Worker B selected
-> B returns invalid output
-> validation rejects
-> retry/fallback policy runs
-> Worker C succeeds
-> canonical story artifact committed once
-> attempt timeline shows A/B/C and reasons
```

UI phai hien duoc timeline/summary toi thieu tu data nay hoac trang run hien co phai duoc mo rong de user hieu fallback da xay ra.

### W1 STOP GATE

Agent **CHI DUOC DUNG** khi tat ca muc sau pass:

- [x] Worker/Driver abstraction hoan tat.
- [x] Pipeline Story khong hardcode vendor.
- [x] Discovery + manual add hoat dong.
- [x] Worker lifecycle hoat dong.
- [x] Model config hoat dong.
- [x] Pool lifecycle + reorder hoat dong.
- [x] Routing config hoat dong.
- [x] Retry/fallback quota/auth/timeout/invalid-output hoat dong.
- [x] Simple cooldown hoat dong.
- [x] Attempt persistence hoat dong.
- [x] Attempt workspace + validation + atomic promote hoat dong.
- [x] Cross-worker safe handoff test pass.
- [x] Routing simulator pass.
- [x] Doctor pass.
- [x] UI Workers/Pool/Routing day du state va lifecycle.
- [x] UI visual review dat yeu cau cua `ui-ux-pro-max-skill`.
- [x] Motion review dat yeu cau cua `gsap-skills`.
- [x] Existing test suite khong regression.
- [x] W1 integration fault-injection scenario pass.

Neu mot checkbox chua pass -> W1 CHUA XONG.

---

# W2 — Production Hardening

> **Chi bat dau W2 khi W1 STOP GATE pass.**
>
> Muc tieu W2: Worker Runtime chay lau dai on dinh, chan storm/retry xau, quan ly shared resources neu use case ton tai, recovery/observability tot hon.

---

## W2.1 Rich health state

### BUG/GAP

W1 health du cho local runtime nhung chua phan biet suy giam theo thoi gian.

### FIX

Mo rong khi co gia tri:

```text
HEALTHY
DEGRADED
AUTH_BLOCKED
QUOTA_BLOCKED
COOLDOWN
UNAVAILABLE
DISABLED
```

### ACCEPTANCE

- [ ] UI va router dung chung mot source of truth.
- [ ] Health transition co timestamp/reason.

---

## W2.2 Proper circuit breaker

### BUG/GAP

Simple cooldown W1 khong du neu provider lien tuc flap.

### FIX

Them:

```text
CLOSED
OPEN
HALF_OPEN
```

Co threshold/cooldown/probe bounded.

### ACCEPTANCE

- [ ] Failure storm khong lam moi task hit provider loi.
- [ ] Half-open probe khong gay concurrency storm.

---

## W2.3 Resource Group — chi implement neu use case that ton tai

### TRIGGER

Chi lam neu co it nhat 2 worker chia se cung account/quota/resource.

### FIX

```text
Worker A -> ResourceGroup X
Worker B -> ResourceGroup X
```

Quota/auth resource block anh huong dung tat ca worker lien quan.

### ACCEPTANCE

- [ ] Shared quota block propagate dung.
- [ ] UI giai thich worker unavailable vi shared resource.

Neu trigger khong ton tai, ghi `NOT NEEDED YET` va khong tao subsystem rong.

---

## W2.4 Timeout refinement

### FIX

Tach khi can:

```text
startup_timeout
idle_timeout
hard_timeout
```

### ACCEPTANCE

- [ ] Moi timeout class co policy hop ly.
- [ ] Kill subprocess tree sach.

---

## W2.5 Reconciler / orphan recovery

### BUG/GAP

App crash khi attempt dang run co the de state treo.

### FIX

Khi startup/recovery:

- tim attempt RUNNING khong hop le;
- kiem tra process/recoverability;
- mark orphan/retry tu checkpoint an toan;
- khong de RUNNING vinh vien.

### ACCEPTANCE

- [ ] Kill ContentFactory giua attempt -> restart -> state duoc reconcile.
- [ ] Canonical artifact khong duplicate/corrupt.

---

## W2.6 Better no-progress protection

### FIX

Them fingerprint bounded cho repeated same failure khi can:

```text
normalized error + work type + checkpoint
```

Neu nhieu worker/attempt lap lai ma khong progress -> stop automation va Needs Attention.

### ACCEPTANCE

- [ ] Poison task khong dot worker vo han.

---

## W2.7 Observability / metrics

Thu toi thieu:

```text
success rate per worker
attempts per task
fallback count
quota/auth/timeout failures
average duration
queue/wait time neu co
```

Khong can telemetry platform moi neu logging/DB hien tai du.

### ACCEPTANCE

- [ ] Co the tra loi "worker nao dang loi nhieu?" ma khong doc raw log.

---

# W2 UI

## W2.UI.1 Worker detail health/history

Them detail surface:

```text
health trend
last success/failure
recent attempts
cooldown/circuit state
quota/reset info neu provider dua timestamp dang tin
```

Khong bien thanh dashboard chart day dac.

---

## W2.UI.2 Run/Attempt timeline

UI run phai cho thay:

```text
Attempt 1  Worker A  QUOTA
Attempt 2  Worker B  INVALID OUTPUT
Attempt 3  Worker C  SUCCESS
```

Default la human-readable timeline; raw logs nam Advanced.

---

## W2.UI.3 Impact preview

Voi thao tac anh huong routing nhu disable worker/delete pool/change shared resource:

```text
Affected pools
Affected work types
Running tasks affected or not
```

### ACCEPTANCE

- [ ] User biet tac dong truoc destructive/config change.

---

## W2.UI.4 Health Center integration

Khong bat buoc them sidebar moi neu co Doctor/Settings area phu hop. Hien thi gon:

```text
workers healthy/degraded
workers auth-required
quota-blocked
circuits open
```

CTA phai co action ro khi user can lam gi.

---

## W2 STOP GATE

- [ ] W1 van pass regression.
- [ ] Rich health state hoat dong.
- [ ] Circuit breaker hoat dong.
- [ ] Resource Group chi co neu co use case that; neu co thi test shared quota pass.
- [ ] Timeout/recovery hardening pass.
- [ ] Restart/orphan recovery scenario pass.
- [ ] No-progress protection pass.
- [ ] Metrics/history du dung cho van hanh.
- [ ] W2 UI health/timeline/impact states dep, de hieu, accessible.
- [ ] UI review lai voi `ui-ux-pro-max-skill` va `gsap-skills`.
- [ ] Full test suite pass.

Neu mot gate chua pass -> W2 CHUA XONG.

---

# W3 — Platform Expansion (chi lam khi product thuc su can)

> W3 khong phai mac dinh. Chi implement hang muc co product trigger that. Muc tieu la tranh bien ContentFactory thanh orchestration platform qua som.

---

## W3.1 Driver plugin loading

### TRIGGER

Can cho ben thu ba/ngoai core them driver ma khong sua source core.

### FIX

Plugin manifest + versioned driver contract + safe loading.

Khong lam plugin marketplace neu chi team noi bo dung.

---

## W3.2 Multi-machine workers

### TRIGGER

Mot may khong du compute/capacity hoac co worker o nhieu node.

### FIX

Luc nay moi can:

```text
node identity
remote execution
lease/fencing formalization
network failure handling
cross-node health
```

Khong duoc implement som trong W1/W2.

---

## W3.3 Adaptive routing

### TRIGGER

Da co du telemetry thuc te va priority/least-busy khong con du.

### FIX

Scoring co the dung:

```text
configured priority
recent reliability
quality results
load
latency
```

Tat ca decision phai explainable.

Khong dung AI router chi de chon AI neu deterministic scoring du.

---

## W3.4 Advanced model routing

### TRIGGER

Can preserve quality profile cross-vendor, dynamic downgrade, model-specific health/quota.

### FIX

Model profile/routing chi tiet hon W1.

---

## W3.5 Workflow editor

### TRIGGER

Nguoi dung can thay doi graph pipeline, them/xoa/reorder stage.

Neu pipeline van co dinh Source -> Story -> TTS -> Render -> Publish, **khong lam** node editor.

---

## W3.6 Team / parallel agents

### TRIGGER

Co bai toan can song song writer/reviewer/researcher va bang chung gia tri > complexity.

Khong lam agent-to-agent chat mac dinh. Control Plane van la source of truth.

---

# W3 UI

Chi thiet ke UI cho feature W3 da duoc kich hoat boi trigger that. Moi feature van phai tuan thu UI/UX contract va hai skill.

Khong them man hinh chi vi backend co abstraction.

---

## W3 STOP GATE

W3 khong co mot gate chung bat buoc tat ca feature. Moi W3 feature duoc kich hoat phai co:

- [ ] Product trigger duoc ghi ro.
- [ ] Scope duoc gioi han.
- [ ] Full lifecycle neu entity moi duoc tao.
- [ ] Failure/recovery path.
- [ ] UI polished neu user-facing.
- [ ] Tests + docs + backward compatibility.
- [ ] Khong lam tang complexity cua W1 core neu khong can.

---

# Phase Comparison — de agent khong lam qua tay

| Area | W1 | W2 | W3 |
|---|---|---|---|
| Vendor-independent Worker/Driver | MUST | Harden | Extend/plugin |
| Discovery | PATH/manual/basic probe | Better diagnostics | Multi-node/plugin |
| Models | default/allowed/profile simple | richer health/resource if needed | adaptive routing |
| Pools | priority/least-busy | richer health-aware | cross-node/dynamic |
| Retry/Fallback | MUST | hardened | distributed/adaptive |
| Quota/Auth/Timeout | MUST | richer states/circuit | enterprise scale only if needed |
| Attempt history | MUST | richer timeline/metrics | event platform only if needed |
| Isolated workspace/validation | MUST | harden recovery | remote sandbox only if needed |
| Resource Group | Not required | only if shared-resource use case | federation if needed |
| Circuit Breaker | simple cooldown | full circuit | distributed if needed |
| Workflow Editor | NO | NO | only with real trigger |
| Team Agents | NO | NO | only with real trigger |
| Multi-machine | NO | NO | only with real trigger |
| UI polish | MUST | MUST | MUST for activated features |

---

# Non-goals — khong tu y them vao W1/W2

Khong implement nhung thu sau neu user/product khong yeu cau ro:

```text
multi-tenant SaaS
RBAC enterprise
plugin marketplace
Kubernetes-style scheduler
multi-machine cluster
agent-to-agent autonomous chat
general visual workflow builder
AI-based routing khi deterministic routing du
full config Git branching/promotion system
archive/restore/history cho moi entity mot cach may moc
```

---

# UI Product Quality Checklist — dung o cuoi moi phase

## Visual

- [ ] Co design system/tokens nhat quan voi UI hien co hoac cai tien co chu dich.
- [ ] Typography hierarchy ro.
- [ ] Spacing theo rhythm, khong random.
- [ ] Card/panel borders/shadows/subtle depth nhat quan.
- [ ] Khong qua lam dung mau status.
- [ ] Iconography nhat quan.
- [ ] Khong co placeholder visual.

## Interaction

- [ ] Primary action ro.
- [ ] Secondary/destructive action khong canh tranh primary.
- [ ] Hover/focus/pressed/disabled state co thiet ke.
- [ ] Add/Edit/Delete/Disable co feedback ngay lap tuc.
- [ ] Async action co progress/disabled state de tranh double-submit.
- [ ] Error gan noi xay ra.
- [ ] Undo cho action reversible neu hop ly.

## Motion

- [ ] Motion co muc dich.
- [ ] Reduced motion hoat dong.
- [ ] Khong block interaction vi animation.
- [ ] GSAP timeline/context cleanup dung.
- [ ] Reorder/add/remove khong giat layout.

## Accessibility

- [ ] Keyboard navigation.
- [ ] Focus visible.
- [ ] Label/icon-button accessible.
- [ ] Contrast du.
- [ ] Status khong phu thuoc mau duy nhat.
- [ ] Modal/drawer focus behavior hop ly.

## Responsive / resilience

- [ ] Desktop viewport nho.
- [ ] Desktop lon.
- [ ] Long worker/model names.
- [ ] Long executable path.
- [ ] Empty lists.
- [ ] Many workers/pools without layout collapse.
- [ ] Slow loading.
- [ ] Partial failure/degraded state.

---

# Final rule for coding agents

**Khong duoc tuyen bo phase da xong vi code compile hoac backend tests pass.**

Mot phase chi xong khi:

```text
Functionality
+ Failure handling
+ Persistence
+ Tests
+ Diagnostics
+ Documentation
+ Polished UI/UX
+ Full relevant lifecycle
+ Acceptance scenarios
= PASS
```

Neu `TARGET_PHASE=W1`, dung lai ngay sau khi W1 STOP GATE pass; khong tu dong lam W2.

Neu `TARGET_PHASE=W2`, bao dam W1 pass, hoan tat W2, sau do dung; khong tu dong lam W3.

Neu `TARGET_PHASE=W3`, chi lam cac W3 feature co product trigger duoc chi dinh ro; khong tu y platform-hoa he thong.


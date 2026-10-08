# ContentFactory — Incremental Dopamine Preservation Rollout

> **Purpose:** This file is the execution contract for the coding agent.
> The agent MUST read this file before changing the Story pipeline and MUST update this same file after every phase, every discovered bug, every fix, every user decision, and every accepted commit.
>
> **Primary constraint:** proceed from the smallest, easiest-to-observe change to the largest architectural change. Do not skip phases. Do not start the next phase without explicit user confirmation.

---

## 0. North Star

ContentFactory processes many different stories. The current `transcript_clean -> story` example is **one benchmark**, not a template that every future story must imitate.

The system must preserve the **dopamine engine of each source**, not hard-code one genre such as rebirth/revenge/face-slapping.

Examples of source-specific reward engines:

- revenge: provocation -> counterattack -> public/status reversal -> consequence
- mystery: question -> clue -> false lead/reversal -> reveal
- horror: unease -> threat -> escalation -> escape/reveal
- romance: attraction -> friction -> emotional progress/reversal -> confirmation
- survival: resource/problem -> danger -> clever response -> temporary safety -> larger danger
- power fantasy/system: task -> gain -> test -> domination/unlock -> larger task
- family drama: injustice -> pressure -> evidence/status shift -> emotional payoff

**Face-slapping is only one possible payoff type.**

The rewrite is successful when a listener who liked the source still likes the rewritten story for the **same underlying emotional reason**, while the rewritten version is cleaner, easier to hear, and better structured.

### Core rule

> Improve prose and audio clarity without silently replacing the source's reward mechanism.

---

## 1. Non-goals

Do NOT optimize all stories toward:

- revenge fiction
- morally simple villains
- female-lead dominance
- constant humiliation scenes
- rebirth tropes
- one fixed sentence-length profile
- one fixed dialogue ratio
- one fixed plot formula

Do NOT assume that "more literary", "more psychologically complex", or "more original" automatically means "better" for this product.

Do NOT delete the existing creative/branching path. A creative branch mode may still be useful for products that intentionally want a new story.

---

## 2. Agent operating contract — mandatory

### 2.1 State machine

Allowed phase states:

- `NOT_STARTED`
- `IN_PROGRESS`
- `WAITING_USER_CONFIRMATION`
- `APPROVED_COMMITTED`
- `REJECTED_ROLLED_BACK`
- `BLOCKED`

The agent MUST keep **Current State** below updated.

### 2.2 Current State

- Active phase: `PHASE 1` (unlocked, not started)
- Status: `NOT_STARTED`
- Last approved commit: `Phase 0 commit (hash recorded below after commit)`
- Current phase base commit: `Phase 0 commit` (Phase 0 base was `6f12443b11054e485a1498afdc0c9f4dd5861392`; branch `feat/dopamine-rollout`, cut from `feat/story-guidance-selective-rerun` HEAD — not from `main`)
- Current benchmark run: `BENCHMARK-001` baseline only (no candidate generated in Phase 0)
- User decision required: `NO` (waiting for instruction to start Phase 1)

### 2.3 Stop rule

For every phase:

1. Record the current Git commit as the phase base.
2. Change only the scope allowed by that phase.
3. Run relevant automated tests.
4. Produce a benchmark candidate using the same source and stable generation settings where possible.
5. Compare candidate vs:
   - source (`transcript_clean` or equivalent source artifact), and
   - previous approved Story output.
6. Update this file with metrics, qualitative differences, regressions, and bugs/fixes.
7. Set phase state to `WAITING_USER_CONFIRMATION`.
8. **STOP. Do not commit yet. Do not start the next phase.**
9. Wait for the user's explicit decision.

Accepted user commands:

- `APPROVE PHASE N` -> mark approved, commit the current phase, record commit hash, then phase N+1 becomes unlocked.
- `REJECT PHASE N` -> restore the working tree to the phase base commit, mark rejected/rolled back, stop.
- `ADJUST PHASE N: ...` -> stay in the same phase, apply only requested adjustment, rerun comparison, stop again.
- `ROLLBACK PHASE N` -> if already committed, use a normal revert commit unless the user explicitly asks for another Git strategy.

### 2.4 Commit rule

- One accepted phase = one focused commit.
- The commit MUST include this file with the updated phase result and commit metadata when practical. If recording the just-created hash inside the same commit is impossible without rewriting history, record it immediately after commit in the working copy and include it in the next approved bookkeeping commit or an allowed tiny metadata commit. Do not amend published history merely to insert the hash.
- Do not squash phase commits while rollout is in progress.
- Do not combine unrelated refactors with a phase.
- Do not force-push.
- Do not use `git reset --hard` for a user-facing rollback unless explicitly requested.

Suggested commit subjects:

- Phase 0: `chore(story): establish dopamine rewrite baseline`
- Phase 1: `fix(story): preserve source reward engine in rewrite prompts`
- Phase 2: `feat(story): add audio dopamine writing guardrails`
- Phase 3: `feat(story): extract generic dopamine profile in shadow mode`
- Phase 4: `feat(story): condition story writing on dopamine profile`
- Phase 5: `feat(story): preserve source payoff beat map`
- Phase 6: `feat(story): add dopamine retention quality report`
- Phase 7: `feat(story): add opt-in dopamine rewrite mode`
- Phase 8: `feat(story): add targeted low-dopamine repair loop`
- Phase 9: `feat(story): roll out dopamine rewrite preset`

### 2.5 Scope guard

Until the phase that explicitly allows it:

- do not change TTS/render behavior;
- do not change model/provider/version during A/B comparison;
- do not change temperature/top-p/seed between baseline and candidate if those controls exist;
- do not replace the current branch/long-write architecture wholesale;
- do not alter multiple independent Story behaviors in the same phase;
- do not optimize only against the current revenge benchmark;
- do not overwrite the last approved output when generating a candidate; keep candidate artifacts separate until approval.

---

## 3. Benchmark policy

### 3.1 The current sample

The current `transcript_clean -> story` case is `BENCHMARK-001`.

It is useful because the source has strong listening dopamine while the current Story rewrite demonstrates a known failure mode: prose/psychology improved, but the emotional engine drifted toward slow suspense and moral ambiguity.

This benchmark is **diagnostic**, not the product specification.

### 3.2 Benchmark corpus growth

Do not make a production-default rollout based on one story.

Target before Phase 9:

- at least 3 heterogeneous stories for a provisional rollout;
- preferably 5+ stories covering different reward engines before making the new mode broadly default.

Each future benchmark must record:

- source genre/reward engine as observed from source;
- source artifact path;
- previous Story output path;
- candidate path;
- generation configuration;
- human notes;
- metric report.

### 3.3 Determinism

If the provider supports a seed, fix it for comparisons.

If no deterministic seed exists:

- Phases 0–6 may use one controlled candidate for fast iteration, but the report must say generation is nondeterministic.
- Phases 7–9 should use multiple runs on important benchmarks when cost permits, so a lucky single generation is not mistaken for a stable improvement.

---

## 4. Generic dopamine model

The system should eventually model source dopamine using generic concepts rather than genre-specific rules.

### 4.1 Reward event taxonomy

A reward event can be any meaningful listener payoff, for example:

- confrontation
- comeback
- status reversal
- punishment/consequence
- revelation
- mystery answer
- clue confirmation
- betrayal reveal
- escape
- survival success
- scare/shock
- romantic progress
- emotional confession
- rejection/reversal
- power gain
- new ability/resource
- achievement/win
- loss/setback that sharply raises stakes
- comic punch
- identity/status reveal
- new threat/open loop

The taxonomy may expand, but must remain generic.

### 4.2 Metrics/rubric

Do not collapse quality into one score. Report the dimensions separately.

Recommended dimensions:

1. **Hook latency** — how long until a meaningful question/conflict/desire appears.
2. **Expectation clarity** — does the listener know what they are waiting to see resolved?
3. **Reward/payoff density** — meaningful payoff events per amount of story.
4. **Longest low-reward span** — longest stretch with setup but no meaningful change/reward.
5. **Escalation quality** — whether new events increase stakes, novelty, or emotional intensity rather than merely repeat.
6. **Reversal quality** — useful changes in who has information, status, leverage, safety, intimacy, etc.
7. **Character agency** — whether major characters actively cause outcomes appropriate to the source's engine.
8. **Emotional clarity** — whether the listener can easily track what to feel/want at a scene level.
9. **Audio clarity** — whether the story remains understandable while listening without visual rereading.
10. **Cognitive friction** — load from names, timelines, hidden information, rules, nested motives, or dense exposition.
11. **Source engine retention** — whether the rewrite rewards the listener in the same fundamental way as the source.
12. **Prose cleanliness** — fluency, repetition, obvious ASR artifacts, awkward transitions, AI-style clutter.

### 4.3 Important interpretation rule

A metric is evidence, not truth.

For example:

- low payoff density may be correct for a slow horror source;
- high moral ambiguity may be correct if the source already depends on ambiguity;
- strong protagonist dominance may be wrong for tragedy or horror;
- many open loops can become confusing rather than addictive.

Therefore every score must be interpreted relative to the **source dopamine profile**.

---

## 5. Standard phase report — mandatory format

At the end of each phase, append/fill a report using this structure:

```text
PHASE N REPORT
Status: WAITING_USER_CONFIRMATION
Base commit: <hash>
Candidate run: <id/path>

Files changed:
- ...

Behavioral change:
- ...

Automated tests:
- PASS/FAIL ...

Benchmark comparison:
| Dimension | Source | Previous approved | Candidate | Notes |
| ... |

What is visibly/audibly better:
1. ...
2. ...

What got worse or is uncertain:
1. ...
2. ...

Source-engine retention:
- ...

Bugs discovered/fixed in this phase:
- BUG-...

Candidate artifacts:
- source: ...
- previous approved: ...
- candidate: ...
- report: ...

Agent recommendation:
- APPROVE / ADJUST / REJECT, with reasons.

USER DECISION REQUIRED.
Do not commit or continue until explicit confirmation.
```

Do not report only "quality improved". Show concrete examples or metrics.

For long stories, include a small number of representative excerpts/locations rather than dumping the entire story into chat.

---

# PHASES

---

## PHASE 0 — Baseline, repository discovery, and rollback harness

### Risk

Very low. No intended Story behavior change.

### Goal

Create a trustworthy baseline so later quality claims are measurable and rollback is trivial.

### Allowed changes

- documentation;
- benchmark fixtures/references;
- non-production comparison/report tooling;
- tests that do not alter generation behavior.

### Required work

1. Inspect the actual repository and fill **Architecture Snapshot** below.
2. Find the exact Story pipeline entrypoint(s).
3. Identify where `story-branch`, `story-long-write`, prompts, presets, assembler, and model configuration are invoked.
4. Record the current Git HEAD.
5. Locate or materialize `BENCHMARK-001` source and current Story output.
6. Freeze the current generation configuration.
7. Add the smallest practical benchmark command/script/test harness that can:
   - run Story generation on a fixed source;
   - preserve the result as a candidate artifact;
   - compare source / approved baseline / candidate;
   - avoid overwriting approved output.
8. Capture baseline qualitative and basic quantitative metrics.
9. Do not alter production Story prompts in this phase.

### Architecture Snapshot — agent must fill

- Story stage entrypoint: `src/contentfactory/story/stage.py:28` `run(ctx, story)`; registered as pipeline stage `story` in `src/contentfactory/jobs/pipeline.py:103` (`params_deps=("story_profile","language","fake")`, `config_deps=("story_branch",)`). Builds `bundle = {title, language, source_language, transcript, [guidance]}` and calls `story.generate(bundle, profile, stage_dir, ctx)`.
- Story orchestration file(s): `src/contentfactory/adapters/story_branch.py` (`StoryBranchAdapter.generate`, l.253); stage glue + assembler/validator in `src/contentfactory/story/{stage,assembler,validate}.py`.
- Prompt/wrapper file(s): ALL ContentFactory-owned prompt text lives in `adapters/story_branch.py`: `HEADLESS` (l.41), `GUIDANCE_RULES` (l.45), `FOLLOW_UP` (l.49), `guidance_block()`, and the per-step prompts inside `generate()` (l.301-317). Phase 1/2 (prompt-only) changes land here. The oh-story skills themselves are NOT ours (`modules/oh-story-claudecode`, pinned `a8dc674`, gitignored) and must not be edited.
- story-branch integration: steps `analyze -> explore -> create -> handoff` (`/story-branch <step>`), each a fresh Claude Code session; done-checks are files on disk (`_done`, l.243). `cre` prefix (HEADLESS + user guidance) is applied to explore/create/handoff/outline/write; `analyze` gets `pre` only.
- story-long-write integration: `/story-long-write 开书` (outline, >=10 detailed chapters) then `写第a-b章` in batches <= 3 chapters until `_tracking-state.json.last_committed_chapter >= chapters`. Defaults: `target_chars=40000`, `chapter_chars=3000` -> 14 chapters (`profile = params["story_profile"]`, empty by default).
- assembler integration: `story/stage.py` -> `assembler.assemble(sections, max_removed_ratio=0.35)` (strips headings/meta/recaps/overlaps/dups; safety net `ASSEMBLER_REMOVED_TOO_MUCH`) -> `validate_story_text` -> `story.txt` + `assembly_report.json`. Reused when section sha256 unchanged. Output contract for downstream TTS: one plain `story.txt`, no chapter headers.
- channel/preset config: no story-specific preset exists today. Only `config["story"]["guidance"]` (D-112, free-text creative guidance, per-job override) and `config["story_branch"]` (`permission_mode, max_turns=80, max_follow_ups=4, chapters_per_batch=3, max_budget_usd_per_turn`). Defaults in `orchestrator/config.py:68`; machine overrides in `config/config.local.json` (gitignored). Adapter chosen by `adapters.story = "story_branch"` (`orchestrator/registry.py:62`; `fake` for tests).
- generation provider/model config: Claude Code CLI headless (`ClaudeCliRunner`), `story_branch.model` UNSET => CLI default model; no temperature/top-p/seed controls exist => generation is NONDETERMINISTIC (job manifest also flags `nondeterministic`). Adapter input fingerprint includes transcript sha, title, languages, book name, `ADAPTER_VERSION="1"`, guidance.
- current test command(s): `python -m unittest discover -s tests -t .` (full, ~8 min); Story-focused: `python -m unittest tests.test_story_bench tests.test_story_branch tests.test_assembler tests.test_story_guidance` (52 tests).
- benchmark command: `python scripts/story_bench.py compare BENCHMARK-001 [--candidate ID]` (metrics only, free); `python scripts/story_bench.py run BENCHMARK-001 --candidate ID --yes` (real generation, costs tokens; refuses to overwrite an existing candidate, never touches `approved/`).
- benchmark artifact directory: `benchmarks/BENCHMARK-001/` = `benchmark.json` (metadata + sha256), `source.txt` (transcript_clean), `approved/story.txt` + `approved/assembly_report.json`, `candidates/<id>/` (created by `run`).

### Acceptance signal

- No intended change in generated Story behavior.
- Baseline can be reproduced or at least rerun under recorded settings.
- Candidate output is separated from approved output.
- Rollback point is known.

### Must stop after report

Yes.

### Phase result

- Status: `APPROVED_COMMITTED`
- Base commit: `6f12443b11054e485a1498afdc0c9f4dd5861392`
- Approved commit: `see Decision Log` (hash recorded right after commit)
- Notes: see PHASE 0 REPORT below.

### PHASE 0 REPORT

```text
PHASE 0 REPORT
Status: APPROVED_COMMITTED
Base commit: 6f12443b11054e485a1498afdc0c9f4dd5861392
Candidate run: none (Phase 0 = baseline only; no generation performed)

Files changed (all NEW, no production file touched):
- benchmarks/BENCHMARK-001/{benchmark.json, source.txt, approved/story.txt, approved/assembly_report.json}
- scripts/story_bench.py        (metrics / compare / run harness, stdlib + existing adapter)
- tests/test_story_bench.py     (2 tests: metric sanity, fixture sha256 integrity)
- Promtps/AGENT_DOPAMINE_ROLLOUT.md (this file)

Behavioral change:
- None. src/ is untouched; Story prompts, assembler, config unchanged.

Automated tests:
- PASS tests.test_story_bench tests.test_story_branch tests.test_assembler tests.test_story_guidance (52 tests, OK).
- Full suite not run (no src change).

Benchmark comparison (BENCHMARK-001, source vs previous approved Story; no candidate yet):
| Dimension | Source | Previous approved | Notes |
| chars / words | 52,558 / 11,946 | 58,293 / 12,884 | similar length: not an over-expansion by size |
| sentence length (words, mean / p90) | 14.8 / 27 | 8.9 / 17 | rewrite is much choppier; source is ASR run-on |
| hook latency (words to first ?/!/quote) | 169 (channel greeting included) | 49 | rewrite hooks faster in text; but see qualitative |
| longest span w/o ?/!/quote (words) | 775 | 290 | proxy only; source has NO quote marks (ASR), so not comparable |
| questions per 1k words | 10.88 | 4.66 | source is more interrogative/confrontational |
| exclamations per 1k words | 0.25 | 0.08 | both low |
| dialogue paragraph ratio | 0.021 (unreliable) | 0.31 | source has no quote markup |
| paragraphs | 47 | 696 | source is a wall of text |
| name-like phrases (rough cognitive-load proxy) | 156 | 160 | roughly equal; regex is approximate |
Metrics are EVIDENCE, not truth: paragraph/dialogue counts are not comparable because source is unpunctuated ASR text.

What is visibly/audibly better (approved Story vs source):
1. Prose cleanliness: source has ASR corruption (e.g. "Từ Phá Nhi / Phán Nhi / Phức", "ngheo rớt mồng tơi", mid-sentence breaks); Story is clean and paragraphed.
2. Scene clarity and sensory grounding (e.g. Story opens at the bedroom door with concrete action instead of a channel greeting).

What got worse or is uncertain (the known failure mode, qualitative, from excerpts):
1. Reward mechanism drift. Source engine = fast provocation -> counterattack -> public reversal -> consequence. Examples in SOURCE: protagonist ends the sponsorship in front of the class ("Xin lỗi nhé, tôi hối hận rồi"), turns the vice-monitor's hypocrisy back on her ("Đạo đức giả à?"), donates the scholarship money to remote-area children via the teacher, and in the final scene disarms the syringe-wielding Giang Ngạn Tứ and has him arrested (drug charge) = clear catharsis. Approved STORY instead opens with a mystery question ("Anh Ba: Nhớ đấy", "Vậy hôm nay hắn sợ cái gì?"), keeps the antagonist ambiguous/sympathetic ("Tôi không biết đó là oán hay là gì khác"), restructures the plot around a debt-slip (giấy nợ, "Khoản 1/88", 4,396 tệ in 50-tệ instalments), and ends with the antagonist handed to family/police quietly ("Không ai lớn tiếng") — quiet resolution instead of public victory.
2. Antagonist rehabilitation / moral inversion: the rival character (Từ Phá Nhi) becomes a dignified debtor repaying in instalments ("Tớ không cần cậu thương. Tớ cần giấy."), a plot element absent from the source.
3. New named entities and subplots (Anh Ba, Tần Nhiên's camera/Hắn's tracking, chú Triệu, 'NT' receipts, giấy nợ) -> more setup that the source never needed, delaying payoffs. Pacing of payoffs is not measured numerically in Phase 0 (no beat map until Phase 5/6).

Source-engine retention:
- Baseline for later phases: LOW-to-MODERATE on this benchmark (human judgment from excerpts; not a formal score). The user's own listening verdict (slow suspense, moral ambiguity) is consistent with this reading.

Bugs discovered/fixed in this phase:
- none in production code. Harness note: my first metric draft used paragraph counts, which were misleading on the single-block ASR source; replaced by sentence/word-based spans (documented above).

Candidate artifacts:
- source: benchmarks/BENCHMARK-001/source.txt  (sha256 in benchmark.json; origin workspace/job_000015/import/from_000014/source/transcript_clean.txt)
- previous approved: benchmarks/BENCHMARK-001/approved/story.txt (identical to output/20261007_truyen-audio-tai-sinh-.../story.txt and job 14/15 story)
- candidate: none yet
- report: this section + `python scripts/story_bench.py compare BENCHMARK-001`

Caveats / open facts for the user:
- Branch base: cut from `feat/story-guidance-selective-rerun` (unmerged work), not `main`. The Phase 0 commit will sit on top of those commits.
- Generation is nondeterministic and the model is the CLI default (not pinned). A/B in Phases 1+ should record the CLI model in each candidate's run.json (the harness stores story_branch config + stats; it does NOT yet record the CLI model — add in Phase 1 if wanted).
- The earlier output `20261006_full-audio-ban-cung-phong-tu-nhan-minh-la-quan-chua-xuyen-kh` is a second story that could become BENCHMARK-002 (different genre); not added in Phase 0 to keep scope minimal.
- Fixtures (~130 KB of text) are committed under benchmarks/ so the baseline survives deleting gitignored workspace/ and output/.

Agent recommendation:
- APPROVE (nothing in production changed; rollback = delete the new files / revert the commit).

USER DECISION REQUIRED.
Do not commit or continue until explicit confirmation.
```

---

## PHASE 1 — Minimal source-reward preservation override

### Risk

Low.

### Goal

Produce the first **immediately audible** improvement using only a small prompt/invocation change, without changing pipeline architecture.

### Design

At the ContentFactory Story invocation layer, add a concise preservation contract roughly equivalent to:

- This is a rewrite unless explicitly requested otherwise.
- Identify the source's primary emotional/reward engine.
- Preserve the major payoff type and general payoff order.
- Do not introduce unreliable memory, moral inversion, antagonist rehabilitation, mystery scaffolding, or a slower psychological engine unless those are already part of the source.
- Improve prose/scene clarity without weakening or delaying major source payoffs.

**Important:** scope this to ContentFactory's rewrite use case. Do not globally damage a reusable creative `story-branch` skill if that skill is supposed to create divergent fiction.

### Explicitly forbidden in this phase

- no new JSON schema;
- no new pipeline stage;
- no new rewrite mode;
- no new validator;
- no assembler rewrite;
- no model change.

### What to listen for in BENCHMARK-001

The candidate should feel less like a slow psychological mystery and more like the source's original fast reward loop, while still being cleaner than raw transcript text.

Do NOT require exact revenge scenes or exact source wording.

### Acceptance signal

Compared with the current approved Story baseline:

- source-engine retention visibly improves;
- fewer major source payoffs are delayed, softened, or converted into a different genre mechanism;
- no material continuity regression;
- audio clarity remains at least as good.

### Must stop after report

Yes.

### Phase result

- Status: `NOT_STARTED` (unlocked by APPROVE PHASE 0)
- Base commit: `TBD`
- Approved commit: `TBD`
- Notes: `TBD`

---

## PHASE 2 — Generic audio-dopamine writing guardrails

### Risk

Low to moderate.

### Goal

Improve listening momentum without adding new architecture.

### Allowed change

Extend the writer/invocation instructions with a **generic** audio-dopamine rubric.

Suggested guidance:

- establish a meaningful desire/problem/question early;
- minimize long spans in which nothing changes;
- reward setup with payoff before listener memory decays;
- after a payoff, create a new meaningful question/threat/desire when appropriate;
- prefer clear causal scene transitions;
- keep exposition attached to conflict, choice, discovery, or consequence;
- avoid adding extra named entities or subplots unless they earn a payoff;
- keep dialogue and action easy to follow by ear;
- preserve the source's own tempo rather than forcing maximum speed.

### Explicitly forbidden in this phase

- no genre-specific `vả mặt` prompt;
- no hard-coded revenge logic;
- no new dopamine-profile artifact;
- no beat-map artifact;
- no automatic repair loop.

### Acceptance signal

- candidate has fewer dead/low-change stretches than Phase 1;
- listening comprehension does not decrease;
- source engine remains preserved;
- prose does not become clipped, breathless, or repetitive merely to increase speed.

### Must stop after report

Yes.

### Phase result

- Status: `LOCKED_UNTIL_PHASE_1_APPROVED`
- Base commit: `TBD`
- Approved commit: `TBD`
- Notes: `TBD`

---

## PHASE 3 — Extract a generic dopamine profile in shadow mode

### Risk

Low to moderate.

### Goal

Teach the system to describe **why a source is addictive** without yet letting that analysis control production output.

### New artifact

Add a structured `dopamine_profile` (name/path may follow project conventions).

Suggested fields, to be adapted to existing schemas:

```json
{
  "primary_reward_engine": "...",
  "secondary_reward_engines": ["..."],
  "listener_promise": "...",
  "hook_pattern": "...",
  "reward_event_types": ["..."],
  "reward_cadence": "...",
  "escalation_pattern": "...",
  "open_loop_pattern": "...",
  "character_agency_pattern": "...",
  "emotional_clarity": "...",
  "cognitive_load": "...",
  "source_specific_must_preserve": ["..."],
  "source_specific_may_change": ["..."],
  "anti_overfit_notes": ["..."]
}
```

### Shadow-mode rule

The profile is generated and reported, but **must not change the Story output yet**.

This is intentional. We first verify that the analyzer understands multiple kinds of dopamine before trusting it as generation input.

### Evaluation

For BENCHMARK-001 verify that it identifies fast confrontation/reversal/consequence as important without concluding that all stories must use those devices.

When more benchmarks exist, verify the profile changes appropriately across genres.

### Acceptance signal

- profile accurately describes the source's reward engine;
- it does not merely restate plot summary;
- it does not hard-code revenge tropes;
- production Story output is unchanged by design.

### Must stop after report

Yes.

### Phase result

- Status: `LOCKED_UNTIL_PHASE_2_APPROVED`
- Base commit: `TBD`
- Approved commit: `TBD`
- Notes: `TBD`

---

## PHASE 4 — Condition Story writing on the dopamine profile

### Risk

Moderate.

### Goal

Use the source-specific profile as an advisory constraint while keeping the existing overall Story pipeline.

### Allowed change

- pass the approved `dopamine_profile` into the Story writer/rewrite context;
- instruct the writer to preserve `must_preserve` characteristics and reward engine;
- keep existing branch/long-write path otherwise intact.

### Explicitly forbidden

- no dedicated rewrite architecture yet;
- no automatic repair;
- no destructive replacement of creative branch mode.

### Acceptance signal

Across available benchmarks:

- candidate behavior varies according to source engine rather than one universal style;
- BENCHMARK-001 retains more of its fast-payoff feel;
- a mystery/horror/romance benchmark, if available, does not become revenge fiction;
- cognitive load does not increase just because a profile exists.

### Must stop after report

Yes.

### Phase result

- Status: `LOCKED_UNTIL_PHASE_3_APPROVED`
- Base commit: `TBD`
- Approved commit: `TBD`
- Notes: `TBD`

---

## PHASE 5 — Source payoff/beat map preservation

### Risk

Moderate to high.

### Goal

Preserve not only general style but the **reward structure** of the source.

### New artifact

Create a compact beat/payoff map. It should not be a full prose outline.

Suggested structure:

```json
{
  "beats": [
    {
      "id": "B01",
      "role": "setup|pressure|choice|reversal|payoff|escalation|open_loop",
      "event": "short factual description",
      "reward_type": "generic reward taxonomy value",
      "intensity": 1,
      "must_preserve_function": true,
      "may_change_surface_details": true,
      "dependencies": []
    }
  ]
}
```

### Preservation rule

The writer may change wording, scene dressing, transitions, and surface details, but it should not silently:

- delete a major source payoff;
- move a payoff so far away that its setup loses force;
- convert a major payoff into pure introspection;
- replace the source's dominant reward type with a different genre engine;
- add many new setup beats that dilute existing payoff cadence.

### Important

Do not require scene-by-scene copying. Preserve **function**, not exact text.

### Acceptance signal

- major source reward beats can be traced from source -> map -> candidate;
- the candidate is still a rewrite, not a paraphrase;
- payoff order/cadence is materially closer to source where that improves listening;
- no noticeable continuity damage.

### Must stop after report

Yes.

### Phase result

- Status: `LOCKED_UNTIL_PHASE_4_APPROVED`
- Base commit: `TBD`
- Approved commit: `TBD`
- Notes: `TBD`

---

## PHASE 6 — Dopamine retention quality report (validator in shadow mode)

### Risk

Moderate.

### Goal

Make quality drift visible before allowing automatic repair.

### New behavior

After Story generation, produce a quality report comparing source/profile/beat map/candidate.

The validator MUST be advisory in this phase. It does not rewrite production output.

### Report should include

- hook latency estimate;
- reward/payoff events found;
- longest low-reward spans;
- major source reward beats preserved/missing/delayed;
- source-engine retention assessment;
- escalation/reversal notes;
- audio clarity/cognitive friction warnings;
- suspected over-expansion or psychological drift;
- genre-overfit warning;
- confidence/uncertainty notes.

### Avoid fake precision

Do not present a single `87.4/100` number as objective truth.

If an aggregate score is useful, always keep dimension scores and evidence next to it.

### Acceptance signal

On known examples, the report detects the kind of drift seen in the current BENCHMARK-001 Story rewrite without simply declaring raw source text superior on every dimension.

### Must stop after report

Yes.

### Phase result

- Status: `LOCKED_UNTIL_PHASE_5_APPROVED`
- Base commit: `TBD`
- Approved commit: `TBD`
- Notes: `TBD`

---

## PHASE 7 — Add an opt-in dedicated `dopamine_rewrite` mode

### Risk

High. First architectural fork.

### Goal

Stop forcing rewrite jobs through a creative-divergence architecture when the user wants source-engine preservation.

### Architecture target

Conceptually:

```text
transcript_clean
    |
    +--> creative_branch  --> existing story-branch / long-write path
    |
    +--> dopamine_rewrite --> source profile + payoff map + rewrite writer
```

Use actual project naming conventions.

### Requirements

- existing creative branch behavior remains available and unchanged as much as possible;
- new mode is **opt-in**, not default;
- mode selection is explicit and testable;
- downstream assembler/TTS contract remains compatible;
- new mode consumes the generic dopamine profile and payoff map;
- no revenge-specific assumptions;
- output artifact format remains compatible with downstream stages.

### Benchmark requirement

Run both modes on the same input:

- current/creative path;
- new dopamine rewrite path.

Report which product goal each serves better.

### Acceptance signal

The new path clearly preserves source reward mechanics better without sacrificing basic prose/audio quality, and the old path remains usable for intentional creative branching.

### Must stop after report

Yes.

### Phase result

- Status: `LOCKED_UNTIL_PHASE_6_APPROVED`
- Base commit: `TBD`
- Approved commit: `TBD`
- Notes: `TBD`

---

## PHASE 8 — Targeted low-dopamine repair loop

### Risk

High.

### Goal

Repair only weak spans instead of regenerating a whole story whenever the validator finds drift.

### Behavior

1. Generate Story once.
2. Run validator.
3. Identify only high-confidence weak spans.
4. Rewrite those spans with local context + source profile + relevant payoff beats.
5. Revalidate continuity and dopamine retention.
6. Stop after a strict small number of repair passes.

### Hard limits

Initial recommendation:

- maximum 1 automatic repair pass;
- no whole-story rewrite inside the repair loop;
- do not repair low-confidence subjective issues automatically;
- do not change already-good spans merely for stylistic uniformity;
- every repair must preserve neighboring continuity.

The exact limits may change after evidence.

### Acceptance signal

- targeted repair improves known weak spans without creating continuity seams;
- cost/latency remains acceptable;
- repeated repair does not homogenize all stories into one style.

### Must stop after report

Yes.

### Phase result

- Status: `LOCKED_UNTIL_PHASE_7_APPROVED`
- Base commit: `TBD`
- Approved commit: `TBD`
- Notes: `TBD`

---

## PHASE 9 — Controlled preset/default rollout

### Risk

Highest product risk because behavior reaches normal production.

### Preconditions

Do not start unless:

- Phases 0–8 accepted or explicitly waived by user;
- benchmark corpus has at least 3 heterogeneous stories, preferably 5+;
- no known critical regression;
- rollback is one config change or one revert;
- cost/latency impact is known.

### Goal

Expose a stable user/channel preset such as:

```text
story.mode = dopamine_rewrite
```

while preserving an explicit creative mode.

Do not assume exact configuration syntax; follow the repository's actual conventions.

### Rollout options

Prefer smallest rollout first:

1. one local/manual preset;
2. one selected channel/profile;
3. broader default only after evidence.

### Acceptance signal

Across heterogeneous benchmarks and at least one realistic end-to-end production run:

- source-engine retention improves consistently;
- no genre collapse toward revenge/vả mặt;
- TTS/downstream compatibility holds;
- cost and latency are acceptable;
- rollback path is verified.

### Must stop after report

Yes.

### Phase result

- Status: `LOCKED_UNTIL_PHASE_8_APPROVED`
- Base commit: `TBD`
- Approved commit: `TBD`
- Notes: `TBD`

---

# 6. Bug -> Fix Log — update continuously

Every bug discovered during this rollout MUST be recorded here, including bugs fixed within the same phase.

Do not delete old rows. Mark status instead.

| Bug ID | Phase found | Symptom | Root cause | Fix | Regression test/evidence | Status | Commit |
|---|---:|---|---|---|---|---|---|
| _example_ | 1 | Candidate ignored source payoff | Conflicting creative-branch instruction | Scoped preservation override in ContentFactory wrapper | BENCHMARK-001 comparison | EXAMPLE ONLY | - |

Status values:

- `OPEN`
- `FIXED_UNCOMMITTED`
- `FIXED_COMMITTED`
- `DEFERRED`
- `WONT_FIX`
- `REGRESSION`

If a bug is caused by an already-approved earlier phase:

1. log the originating phase;
2. fix it in the current allowed phase if scope permits;
3. if it blocks progress and cannot fit current scope, stop as `BLOCKED` and ask the user;
4. never silently rewrite previous Git history.

---

# 7. Decision Log — append only

| Date | Phase | Decision | User instruction | Result/commit |
|---|---:|---|---|---|
| TBD | 0 | Plan created | Incremental phases, stop/report/confirm/commit each phase | TBD |
| 2026-10-08 | 0 | Branch `feat/dopamine-rollout` created from `feat/story-guidance-selective-rerun` @ 6f12443; Phase 0 executed, awaiting approval | "chia nhánh và chạy AGENT_DOPAMINE_ROLLOUT.md" | executed |
| 2026-10-08 | 0 | APPROVE PHASE 0; commit Phase 0 | "approve" | commit hash: TBD |

---

# 8. Phase Quality History — append one row after every candidate

| Phase | Benchmark | Source-engine retention | Audio clarity | Reward cadence | Cognitive friction | Continuity | Human verdict | Candidate path |
|---:|---|---|---|---|---|---|---|---|
| 0 | BENCHMARK-001 | Low-to-moderate (approved Story drifts to suspense/ambiguity; qualitative) | Good (clean paragraphs, short sentences) | Slower than source: payoffs delayed/softened, quiet ending | Higher setup load (new entities/subplots: Anh Ba, giấy nợ, ...) | No known break | approved | benchmarks/BENCHMARK-001/approved/story.txt (baseline) |

Use qualitative values or documented rubric scales consistently. Do not fabricate precision.

---

# 9. Rollback protocol

### Before approval

The phase should still be uncommitted.

If rejected:

- preserve the report/candidate if useful for diagnosis;
- restore code/config changes to the recorded phase base;
- mark phase `REJECTED_ROLLED_BACK`;
- update Decision Log;
- stop.

### After approval and commit

If the user later requests rollback:

- prefer `git revert <phase-commit>` so history remains auditable;
- record the revert commit in Decision Log and Bug/Fix Log if applicable;
- rerun smoke tests;
- stop for user confirmation before attempting an alternative implementation.

---

# 10. Quality philosophy for the coding agent

When judging candidate Story output, remember:

### Wrong optimization

> Make the story more sophisticated, more literary, more psychologically complex, or more original at all costs.

### Correct optimization

> Preserve what makes this particular source hard to stop listening to, then improve clarity, prose, causality, and audio delivery without destroying that mechanism.

A rewrite can be "better written" and still be a product regression.

A raw source can be clumsy and still have a stronger reward loop.

The system's job is to preserve the latter while improving the former.

---

# 11. Immediate instruction to the next coding agent

Start **PHASE 0 only**.

Do not implement Phase 1 yet.

First:

1. inspect the repository;
2. fill the Architecture Snapshot with real paths/commands;
3. capture the current Git HEAD;
4. establish BENCHMARK-001 source/baseline artifacts;
5. add only the minimum non-production benchmark/report harness needed;
6. run tests and baseline analysis;
7. update this file;
8. report the findings to the user using the Standard Phase Report;
9. set status to `WAITING_USER_CONFIRMATION`;
10. stop.

**Do not commit Phase 0 until the user explicitly says `APPROVE PHASE 0`.**


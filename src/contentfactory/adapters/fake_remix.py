"""LLM giả cho Story Remix (adapter `fake`/test): trả JSON/văn bản hợp lệ theo từng bước, đọc character_id từ prompt; ghi lại prompt để kiểm tra cô lập nguồn."""
from __future__ import annotations

import json
import random
import re

_WORDS = ("đêm khuya gió mưa hẻm nhỏ bước chân xa dần ngọn đèn vàng cánh cửa gỗ tiếng động lạ người đàn ông áo đen im lặng bóng tối kéo dài lá khô rơi con mèo trắng bờ sông lạnh "
          "sương mù tiếng chuông chùa vọng lại tấm bản đồ cũ chiếc chìa khóa gỉ lá thư chưa gửi con phố vắng ánh trăng nhợt nhạt mùi hương lạ bàn tay run rẩy lời hứa bị quên").split()

DNA = {"genre": "báo thù, phản đòn", "subgenres": ["trả thù"], "engagement_engine": "người nghe chờ khoảnh khắc kẻ ác bị vạch mặt và trả giá; mỗi vài chương lại có một cú phản đòn thỏa mãn",
       "reward_types": ["phản đòn vạch mặt", "kẻ ác trả giá", "đảo ngược vị thế"], "emotional_promise": "công lý được thực thi một cách hả hê", "hook_pattern": "mở đầu bằng cú phản bội gây sốc",
       "payoff_cadence": {"first_payoff_by_pct": 15, "payoffs_per_10pct": 1.0}, "escalation_pattern": "mỗi lần phản đòn lớn hơn, kẻ thù mạnh hơn", "pacing": "nhanh, chương ngắn", "tone": "căng thẳng, hả hê",
       "pov": "ngôi thứ nhất", "audio_requirements": ["câu ngắn", "tên nhân vật khác nhau rõ"], "avoid": ["lê thê"]}


GENRES = {
    "horror": (("ma", "tiếng gõ", "thì thầm", "rùng rợn", "bóng đen", "tầng hầm"), {**DNA, "genre": "kinh dị", "subgenres": ["ma ám"], "engagement_engine": "nỗi sợ được đẩy lên từng bước rồi giải tỏa bằng một cú thoát hiểm; người nghe chờ thực thể được hé lộ",
               "reward_types": ["sợ hãi leo thang", "hé lộ thực thể", "thoát hiểm trong gang tấc"], "emotional_promise": "nỗi sợ tăng dần rồi được giải tỏa", "hook_pattern": "mở đầu bằng một dấu hiệu rùng rợn trong đêm",
               "payoff_cadence": {"first_payoff_by_pct": 20, "payoffs_per_10pct": 0.8}, "escalation_pattern": "mỗi đêm một dấu hiệu đáng sợ hơn", "tone": "u ám, rùng rợn"}),
    "mystery": (("thám tử", "vụ án", "manh mối", "nghi phạm", "hung thủ"), {**DNA, "genre": "trinh thám", "subgenres": ["phá án"], "engagement_engine": "người nghe cùng ráp từng manh mối và chờ khoảnh khắc đảo ngược nghi phạm trước lời giải",
               "reward_types": ["manh mối mới", "đảo ngược nghi phạm", "giải mã vụ án"], "emotional_promise": "bí ẩn được giải bằng logic thỏa mãn", "hook_pattern": "mở đầu bằng một vụ mất tích bí ẩn",
               "payoff_cadence": {"first_payoff_by_pct": 15, "payoffs_per_10pct": 0.9}, "escalation_pattern": "mỗi manh mối đổi hướng vụ án", "tone": "tỉnh táo, căng thẳng"}),
    "romance": (("tình yêu", "rung động", "tỏ tình", "hiểu lầm", "nụ cười"), {**DNA, "genre": "tình cảm", "subgenres": ["lãng mạn"], "engagement_engine": "người nghe mong hai nhân vật vượt qua hiểu lầm để đến với nhau",
               "reward_types": ["khoảnh khắc rung động", "hiểu lầm được hóa giải", "lời tỏ tình"], "emotional_promise": "tình cảm ngọt ngào và trọn vẹn", "hook_pattern": "mở đầu bằng cuộc gặp tình cờ",
               "payoff_cadence": {"first_payoff_by_pct": 18, "payoffs_per_10pct": 0.7}, "escalation_pattern": "tình cảm sâu dần qua từng thử thách", "tone": "ấm áp, dịu dàng"}),
    "comedy": (("hài hước", "gây cười", "cười", "nhầm lẫn", "tếu"), {**DNA, "genre": "hài hước", "subgenres": ["tình huống"], "engagement_engine": "chuỗi tình huống nhầm lẫn tăng dần rồi một cú đảo ngược gây cười",
               "reward_types": ["tình huống nhầm lẫn gây cười", "đảo ngược hài hước", "kết thúc ấm áp"], "emotional_promise": "tiếng cười thoải mái và kết thúc ấm lòng", "hook_pattern": "mở đầu bằng một nhầm lẫn ngớ ngẩn",
               "payoff_cadence": {"first_payoff_by_pct": 10, "payoffs_per_10pct": 1.2}, "escalation_pattern": "mỗi nhầm lẫn kéo theo một nhầm lẫn lớn hơn", "tone": "vui nhộn, nhẹ nhàng"}),
}


def detect_dna(text: str) -> dict:
    """Chọn DNA giả theo từ khóa trong transcript (chỉ cho fake/test; LLM thật tự phân tích)."""
    low = text.lower()
    hits = lambda kws: sum(len(re.findall(r"(?<!\w)" + re.escape(k) + r"(?!\w)", low)) for k in kws)                 # noqa: E731 - khớp nguyên từ ("ma" không khớp "mà")
    best = max(GENRES.items(), key=lambda kv: hits(kv[1][0]))
    return best[1][1] if hits(best[1][0]) >= 3 else DNA


def reward_types_in(prompt: str) -> list[str]:
    m = re.search(r'"reward_types":\s*\[(.*?)\]', prompt, re.S)
    try:
        return json.loads("[" + m.group(1) + "]") if m else DNA["reward_types"]
    except ValueError:
        return DNA["reward_types"]


def premise(pid: str, *, weak: bool = False, copy_names: bool = False, reward_types: list | None = None) -> dict:
    rt = reward_types or DNA["reward_types"]
    long = "" if weak else " với nhiều lớp mưu mẹo và hậu quả kéo dài khiến mọi quyết định đều phải trả giá"
    hero = "Lý Hoàng" if copy_names else "Kiều An"
    return {"id": pid, "logline": f"{hero} là kế toán trẻ bị đồng nghiệp đẩy vào vụ gian lận{long}", "setting": "một công ty logistics ở thành phố cảng",
            "central_conflict": ("tranh chấp nhỏ" if weak else f"{hero} phải tự chứng minh mình trong sạch trước khi bị khởi tố, trong khi kẻ chủ mưu nắm trong tay mọi bằng chứng giả"),
            "stakes": "tự do và danh dự" if weak else "tự do, danh dự và khoản tiền cứu mẹ đang bệnh nặng", "twist": "ngắn" if weak else "người cố vấn đáng tin hóa ra là kẻ thao túng sổ sách từ đầu",
            "ending": "ok" if weak else "kẻ chủ mưu bị vạch mặt trước hội đồng, còn Kiều An giành lại vị trí và công lý", "hook": "đêm cuối cùng trước khi bị khởi tố, một tin nhắn lạ xuất hiện" if not weak else "x",
            "slots": [{"slot_id": "hero", "role_code": "protagonist", "importance": 3, "traits": ["kiên trì", "tỉ mỉ"], "goal": "chứng minh mình trong sạch"},
                      {"slot_id": "villain", "role_code": "antagonist", "importance": 3, "traits": ["tham vọng", "đa nghi"], "goal": "che giấu gian lận", "relationships": [{"with": "hero", "type": "enemy_of"}]},
                      {"slot_id": "ally", "role_code": "ally", "importance": 2, "traits": ["trung thành"], "goal": "giúp Kiều An tìm chứng cứ", "relationships": [{"with": "hero", "type": "friend_of"}]}],
            "payoff_plan": [{"beat": f"{beat} ({rt[i % len(rt)]})", "at_pct": pct, "type": rt[i % len(rt)]} for i, (beat, pct) in enumerate(
                [("điểm thưởng đầu tiên của hành trình", 12), ("điểm thưởng thứ hai", 35), ("điểm thưởng đảo chiều", 60), ("điểm thưởng lớn", 85), ("điểm thưởng cuối", 97)])]}


class FakeRemixLLM:
    """Cấu hình: premises_by_call[i] = list premise dict cho lần gọi premises thứ i; review = verdict; outline_gap = số chương giữa các payoff (dùng để thử cổng dopamine)."""

    def __init__(self, premises_by_call=None, review="distinct", review_overlaps=None, outline_gap=2, cost=0.5, report_cost=True, outline_repair_gap=None, dna=None,
                 chapter_behavior=None, genre_aware=False):
        self.premises_by_call = premises_by_call or [[premise("P1"), premise("P2", weak=True), premise("P3", weak=True)]]
        self.review, self.review_overlaps, self.outline_gap, self.cost, self.report_cost = review, review_overlaps or [], outline_gap, cost, report_cost
        self.outline_repair_gap = outline_repair_gap
        self.chapter_behavior = chapter_behavior or {}                 # {n: callable(first_attempt:int, names:list[str], target:int) -> (text, memory_update dict)}
        self.dna = dna or DNA
        self.genre_aware = premises_by_call is None or genre_aware
        self.calls: list[dict] = []
        self.counts: dict[str, int] = {}

    def complete(self, prompt, *, system, step, ctx=None):
        self.calls.append({"step": step, "prompt": prompt})
        n = self.counts[step] = self.counts.get(step, 0) + 1
        if step.startswith("chapter_"):
            text = self._chapter(prompt, step, n)
        else:
            text = "```json\n" + json.dumps(getattr(self, "_" + step)(prompt, n), ensure_ascii=False) + "\n```"
        return {"text": text, "cost_usd": self.cost if self.report_cost else None, "tokens_in": len(prompt) // 4, "tokens_out": 500, "seconds": 0.01}

    def _source_dna(self, prompt, n):
        return self.dna if self.dna is not DNA else detect_dna(prompt)

    def _premises(self, prompt, n):
        i = min(n, len(self.premises_by_call)) - 1
        cands = self.premises_by_call[i]
        if self.genre_aware:                                           # mặc định: ý tưởng bám đúng cơ chế thưởng trong DNA của prompt (để thử đa thể loại)
            rt = reward_types_in(prompt)
            cands = [premise(c["id"], weak=len(c["twist"]) < 10, reward_types=rt) for c in cands]
        return {"candidates": cands}

    def _story_bible(self, prompt, n):
        ids = re.findall(r"^- (ch_[0-9a-f]{12}) \|", prompt, re.M)
        return {"title": "Truyện thử", "premise_id": "P1", "setting": {"place": "thành phố cảng", "time": "hiện đại"}, "world_rules": ["mọi bằng chứng đều có dấu vết"],
                "cast": [{"character_id": c, "arc": "thay đổi qua biến cố", "secrets": ["một bí mật"], "voice_notes": "nói riêng biệt"} for c in ids],
                "causal_chain": [{"event": f"sự kiện {i}", "cause": f"nguyên nhân {i}", "effect": f"hệ quả {i}"} for i in range(6)], "themes": ["công lý"], "originality_notes": ["bối cảnh văn phòng"]}

    def _outline(self, prompt, n, repair=False):
        ids = re.findall(r"^- (ch_[0-9a-f]{12}) \| [^|]+\| vai (\w+)", prompt, re.M)
        by_role = {r: c for c, r in ids}
        lo, hi = map(int, re.search(r"ĐẠI CƯƠNG (\d+)–(\d+)", prompt).groups())
        total = (lo + hi) // 2
        gap = (self.outline_repair_gap if repair and self.outline_repair_gap else self.outline_gap)
        chapters = []
        for i in range(1, total + 1):
            cast = [by_role["protagonist"], by_role["antagonist"]] if i % 2 else [by_role["protagonist"], by_role["ally"]]
            if i == 1:
                cast = [by_role["protagonist"], by_role["antagonist"], by_role["ally"]]
            pay = None
            if i % gap == 1 % gap or i == total or i == 1:
                rts = reward_types_in(prompt)
                pk = sum(1 for c in chapters if c["payoff"])                       # loại thưởng xoay vòng THEO THỨ TỰ các điểm thưởng ⇒ phủ đủ mọi loại của DNA
                pay = {"type": rts[pk % len(rts)], "description": f"điểm thưởng: {rts[pk % len(rts)]}"}
            chapters.append({"n": i, "title": f"Chương {i}", "goal": f"mục tiêu chương {i}", "beats": [f"sự kiện {i}.{k} dẫn tới hệ quả khác" for k in range(3)], "cast": cast, "hook": f"hook chương {i}",
                             "payoff": pay, "cliffhanger": "một bí ẩn mới"})
        return {"chapters": chapters}

    def _outline_repair(self, prompt, n):
        return self._outline(prompt, n, repair=True)

    def _originality_review(self, prompt, n):
        return {"verdict": self.review, "overlaps": self.review_overlaps}

    def _chapter(self, prompt, step, call_no):
        repair = step.endswith("_repair")
        n = int(re.search(r"chapter_(\d+)", step).group(1))
        members = re.findall(r"^- (ch_[0-9a-f]{12}) \| ([^|]+)\| vai (\w+)", prompt, re.M)
        names = [m[1].strip() for m in members]
        target = int(re.search(r"≈ (\d+) ký tự", prompt).group(1))
        beh = self.chapter_behavior.get(n)
        if beh:
            r = beh(1 if repair else 0, names, target)
            if r is not None:
                text, upd = r
                return text + "\n###MEMORY###\n" + json.dumps(upd, ensure_ascii=False)
        rnd = random.Random(n * 7919)
        paras, size, k = [], 0, 0
        while size < target * 1.0:
            k += 1
            who = names[(n + k) % len(names)]
            body = " ".join(rnd.choice(_WORDS) for _ in range(34))
            para = f"{who} {body}. {names[(n + k + 1) % len(names)]} đáp lại {' '.join(rnd.choice(_WORDS) for _ in range(12))}."
            paras.append(para)
            size += len(para)
        upd = {"new_facts": [f"chương {n}: {names[0]} tiến thêm một bước"], "state_changes": [{"character_id": members[0][0], "status": "sống", "location": f"nơi {n}"}],
               "opened": [f"bí ẩn {n}"], "resolved": [f"bí ẩn {n - 1}"] if n > 1 else [], "new_named_persons": []}
        return "\n\n".join(paras) + "\n###MEMORY###\n" + json.dumps(upd, ensure_ascii=False)

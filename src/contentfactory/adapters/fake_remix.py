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


def premise(pid: str, *, weak: bool = False, copy_names: bool = False) -> dict:
    long = "" if weak else " với nhiều lớp mưu mẹo và hậu quả kéo dài khiến mọi quyết định đều phải trả giá"
    hero = "Lý Hoàng" if copy_names else "Kiều An"
    return {"id": pid, "logline": f"{hero} là kế toán trẻ bị đồng nghiệp đẩy vào vụ gian lận{long}", "setting": "một công ty logistics ở thành phố cảng",
            "central_conflict": ("tranh chấp nhỏ" if weak else f"{hero} phải tự chứng minh mình trong sạch trước khi bị khởi tố, trong khi kẻ chủ mưu nắm trong tay mọi bằng chứng giả"),
            "stakes": "tự do và danh dự" if weak else "tự do, danh dự và khoản tiền cứu mẹ đang bệnh nặng", "twist": "ngắn" if weak else "người cố vấn đáng tin hóa ra là kẻ thao túng sổ sách từ đầu",
            "ending": "ok" if weak else "kẻ chủ mưu bị vạch mặt trước hội đồng, còn Kiều An giành lại vị trí và công lý", "hook": "đêm cuối cùng trước khi bị khởi tố, một tin nhắn lạ xuất hiện" if not weak else "x",
            "slots": [{"slot_id": "hero", "role_code": "protagonist", "importance": 3, "traits": ["kiên trì", "tỉ mỉ"], "goal": "chứng minh mình trong sạch"},
                      {"slot_id": "villain", "role_code": "antagonist", "importance": 3, "traits": ["tham vọng", "đa nghi"], "goal": "che giấu gian lận", "relationships": [{"with": "hero", "type": "enemy_of"}]},
                      {"slot_id": "ally", "role_code": "ally", "importance": 2, "traits": ["trung thành"], "goal": "giúp Kiều An tìm chứng cứ", "relationships": [{"with": "hero", "type": "friend_of"}]}],
            "payoff_plan": [{"beat": "Kiều An vạch mặt tay sai đầu tiên bằng chứng cứ", "at_pct": 12, "type": "phản đòn vạch mặt"}, {"beat": "kẻ ác trả giá đầu tiên", "at_pct": 35, "type": "kẻ ác trả giá"},
                            {"beat": "đảo ngược vị thế trước hội đồng", "at_pct": 60, "type": "đảo ngược vị thế"}, {"beat": "phản đòn lớn vạch mặt kẻ chủ mưu", "at_pct": 85, "type": "phản đòn vạch mặt"},
                            {"beat": "kẻ ác trả giá cuối cùng", "at_pct": 97, "type": "kẻ ác trả giá"}]}


class FakeRemixLLM:
    """Cấu hình: premises_by_call[i] = list premise dict cho lần gọi premises thứ i; review = verdict; outline_gap = số chương giữa các payoff (dùng để thử cổng dopamine)."""

    def __init__(self, premises_by_call=None, review="distinct", review_overlaps=None, outline_gap=2, cost=0.5, report_cost=True, outline_repair_gap=None, dna=None,
                 chapter_behavior=None):
        self.premises_by_call = premises_by_call or [[premise("P1"), premise("P2", weak=True), premise("P3", weak=True)]]
        self.review, self.review_overlaps, self.outline_gap, self.cost, self.report_cost = review, review_overlaps or [], outline_gap, cost, report_cost
        self.outline_repair_gap = outline_repair_gap
        self.chapter_behavior = chapter_behavior or {}                 # {n: callable(first_attempt:int, names:list[str], target:int) -> (text, memory_update dict)}
        self.dna = dna or DNA
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
        return self.dna

    def _premises(self, prompt, n):
        i = min(n, len(self.premises_by_call)) - 1
        return {"candidates": self.premises_by_call[i]}

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
                pay = {"type": ["phản đòn vạch mặt", "kẻ ác trả giá", "đảo ngược vị thế"][i % 3], "description": "một cú phản đòn thỏa mãn"}
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

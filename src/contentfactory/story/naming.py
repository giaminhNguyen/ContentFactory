"""Tự đặt tên truyện khi người dùng để trống: LLM đọc đại cương (hoặc truyện) đã thiết kế và nghĩ một tên mới (không dùng lại tên video nguồn).

Không bao giờ cắt độ dài: tên quá dài thì Metadata Builder báo TITLE_TOO_LONG như mọi tên khác.
"""
from __future__ import annotations

import re

from ..contracts import clean_title
from ..storyprose import brand_marks  # noqa: F401

SYSTEM = "Bạn là biên tập viên kênh truyện audio YouTube. Chỉ trả về MỘT dòng duy nhất là tiêu đề, không giải thích, không ngoặc kép."
MAX_ATTEMPTS = 3
# Prompt do người dùng cung cấp (2026-10-10). Ngân sách ký tự do CODE tính từ mẫu tiêu đề kênh (LLM đếm ký tự không tin được) và code kiểm lại.
PROMPT = """Dựa trên dàn ý truyện, hãy tạo MỘT tiêu đề hấp dẫn theo phong cách truyện drama viral trên YouTube.

**Ngân sách độ dài (bắt buộc):** tiêu đề video = tiền tố "{prefix}" + tiêu đề của bạn, tổng tối đa 90 ký tự (tính cả khoảng trắng và dấu câu).
Tiền tố đã chiếm {prefix_len} ký tự ⇒ tiêu đề của bạn TỐI ĐA {budget} ký tự. Sáng tạo tiêu đề hoàn chỉnh vừa ngân sách ngay từ đầu;
KHÔNG cắt chuỗi, xoá từ ở cuối hay rút gọn máy móc. Tận dụng hợp lý số ký tự cho phép nhưng không cố lấp đầy.

**Quy trình tư duy:**
1. Xác định tình tiết đắt giá nhất: biến cố, hành động quyết liệt, xung đột, bí mật hoặc cú đảo ngược nổi bật.
2. Chọn góc khai thác tạo cảm xúc mạnh nhất: tò mò, phẫn nộ, hả hê, bất ngờ hoặc tiếc nuối.
3. Biến tình tiết đó thành tiêu đề có tình huống cụ thể, hành động rõ ràng và điểm nhấn kịch tính.

**Loại bỏ watermark và nội dung thừa (làm TRƯỚC khi sáng tạo):** nhận diện và bỏ mọi thông tin không thuộc nội dung truyện: watermark, tên kênh cũ,
thương hiệu, slogan, nguồn đăng tải, lời quảng cáo, lời kêu gọi theo dõi, đoạn chèn nhận diện kênh gốc.
- Không sao chép, biến thể hay đưa các thông tin này vào tiêu đề, kể cả khi chúng nằm trong tên gốc, dàn ý hoặc nội dung truyện.
- Phân biệt tên riêng thuộc cốt truyện với tên kênh/watermark theo ngữ cảnh, tránh xoá nhầm chi tiết quan trọng.
- Chỉ dùng tình tiết thực sự thuộc cốt truyện. Trước khi xuất, tự kiểm tra tiêu đề không còn dấu vết quảng bá/nhận diện nào của nguồn cũ.

**Yêu cầu:**
- KHÔNG tóm tắt dàn ý, liệt kê sự kiện hay đặt tên chung chung.
- Ưu tiên hành động và hậu quả cụ thể thay vì khái niệm trừu tượng.
- Linh hoạt cấu trúc theo nội dung, không rập khuôn công thức.
- Giữ đúng tình tiết truyện, không bịa thêm.
- Ưu tiên ngôi kể "TÔI" nếu phù hợp.
- Tiêu đề tự nhiên, thu hút, viết IN HOA, không mang tính tóm tắt.
- KHÔNG dùng lại hay sao chép tên gốc: "{source_title}".
- Ngôn ngữ: {language}.

Chỉ xuất ra MỘT tiêu đề tốt nhất.

Dàn ý truyện:
{story}
"""


def parse_title(text: str) -> str | None:
    """Dòng không rỗng đầu tiên, bỏ nhãn "Tên truyện:" và ngoặc/dấu trang trí; rỗng => None."""
    line = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    if ":" in line and line.split(":", 1)[0].strip().lower() in ("tên truyện", "tên", "title", "tiêu đề"):
        line = line.split(":", 1)[1]
    line = line.strip().strip("\"'“”‘’«»*")
    return clean_title(line) if line else None


def make_titler(llm):
    """Trả callable(outline, bundle, ctx, prefix, budget) -> tên truyện | None, dùng `llm.complete` (TextLLM).
    Vượt `budget` ký tự thì yêu cầu SÁNG TÁC LẠI (tối đa MAX_ATTEMPTS lượt); không bao giờ cắt. Hết lượt vẫn dài: trả bản ngắn nhất (output báo TITLE_TOO_LONG)."""
    def title(outline: str, bundle: dict, ctx, prefix: str, budget: int) -> str | None:
        src = clean_title(bundle.get("title") or "").lower()
        marks = brand_marks(bundle.get("title") or "")
        prompt = PROMPT.format(language=bundle.get("language") or "vi", source_title=bundle.get("title") or "", story=outline,
                               prefix=prefix, prefix_len=len(prefix), budget=budget)
        best = None
        for _ in range(MAX_ATTEMPTS):
            t = parse_title(llm.complete(prompt, system=SYSTEM, step="story_title", ctx=ctx).get("text") or "")
            if not t or t.lower() == src:                                       # trùng tên gốc = không nghĩ được tên mới
                continue
            hit = next((m for m in marks if m.lower() in t.lower()), None)
            if hit:                                                             # lọt dấu vết kênh gốc: bắt sáng tác lại, không tự xoá
                prompt += f"\n\nTiêu đề vừa rồi \"{t}\" còn dấu vết nguồn cũ \"{hit}\". Sáng tác lại, không chứa cụm này."
                continue
            if len(t) <= budget:
                return t
            best = t if best is None or len(t) < len(best) else best
            prompt += f"\n\nTiêu đề vừa rồi \"{t}\" dài {len(t)} ký tự, VƯỢT ngân sách {budget}. Sáng tác lại một tiêu đề mới ngắn hơn (không cắt bớt tiêu đề cũ)."
        return best
    return title

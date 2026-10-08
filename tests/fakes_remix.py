"""Re-export LLM giả Story Remix + transcript nguồn mẫu (nhiều tên riêng lặp lại để thử phát hiện sao chép)."""
from contentfactory.adapters.fake_remix import DNA, FakeRemixLLM as RemixFakeLLM, premise      # noqa: F401

SOURCE = (
    "Lý Hoàng bị anh em kết nghĩa Tống Mai hãm hại, rơi xuống vực. Lý Hoàng tỉnh dậy, nhớ lại bữa tiệc ở biệt thự Hắc Long nơi Tống Mai đã bỏ thuốc vào rượu. "
    "Lý Hoàng thề sẽ trả thù. Lý Hoàng gặp lão Trần ở làng Đá Xanh, lão Trần dạy Lý Hoàng võ công. Lý Hoàng trở về thành phố, từng bước vạch trần Tống Mai trước mặt mọi người. "
    "Tống Mai quỳ xuống xin tha nhưng Lý Hoàng không tha. Cuối cùng Lý Hoàng lấy lại biệt thự Hắc Long và công ty của gia đình. " * 6)

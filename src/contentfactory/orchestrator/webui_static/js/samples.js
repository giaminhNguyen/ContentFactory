// Tạo dữ liệu mẫu (truyện, phụ đề, audio, video nền) cho người chưa có gì để thử. Dùng chung cho màn hình Chạy và Video nguồn.
import { api } from "./api.js";
import { busy, toast, toastError } from "./components.js";

export async function createSamples(button) {
  return busy(button, async () => {
    try {
      const r = await api.post("/api/samples");
      const bits = [`${r.created.length} file mới`, r.skipped.length ? `${r.skipped.length} file đã có` : null, ...r.registered.map((x) => `đã đăng ký ${x}`)].filter(Boolean);
      toast({ title: "Đã tạo dữ liệu mẫu", message: bits.join(" · "), tone: "done" });
      for (const w of r.warnings) toast({ title: "Lưu ý", message: w, tone: "wait", sticky: true });
      return r;
    } catch (e) { toastError(e, "Không tạo được dữ liệu mẫu"); return null; }
  });
}

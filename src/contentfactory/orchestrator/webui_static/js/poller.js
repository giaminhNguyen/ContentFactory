// Polling có kỷ luật: không chồng yêu cầu, tạm dừng khi tab ẩn, nhanh khi có việc đang chạy, chậm khi rảnh, lùi dần khi lỗi, huỷ sạch khi view đóng.
// fn(signal) trả "fast" | "idle" để chọn nhịp tiếp theo. Mọi view tạo poller trong mount() và gọi stop() trong destroy() (không timer/listener mồ côi).
export const net = new EventTarget();
let online = true;
export const isOnline = () => online;
function setOnline(v) { if (v !== online) { online = v; net.dispatchEvent(new Event("change")); } }

export function createPoller(fn, { fast = 1500, idle = 6000, max = 20000 } = {}) {
  let stopped = true, timer = null, ctrl = null, inflight = false, errors = 0;

  async function tick() {
    if (stopped || inflight) return;
    if (document.hidden) return;                                   // tab ẩn: không gọi API; visibilitychange sẽ gọi lại ngay khi hiện
    inflight = true;
    ctrl = new AbortController();
    let hint = "idle";
    try {
      hint = (await fn(ctrl.signal)) || "idle";
      errors = 0;
      setOnline(true);
    } catch (e) {
      if (e && e.name === "AbortError") { inflight = false; return; }
      errors = Math.min(errors + 1, 6);
      if (e && e.network) setOnline(false);
    }
    inflight = false;
    if (stopped) return;
    const base = hint === "fast" ? fast : idle;
    timer = setTimeout(tick, errors ? Math.min(max, base * 2 ** errors) : base);
  }

  const onVisible = () => { if (!document.hidden && !stopped) { clearTimeout(timer); tick(); } };
  return {
    start() {
      if (!stopped) return;
      stopped = false;
      document.addEventListener("visibilitychange", onVisible);
      tick();
    },
    poke() { if (!stopped) { clearTimeout(timer); tick(); } },       // sau một hành động của người dùng: cập nhật ngay
    stop() {
      stopped = true;
      clearTimeout(timer);
      ctrl?.abort();
      document.removeEventListener("visibilitychange", onVisible);
    },
  };
}

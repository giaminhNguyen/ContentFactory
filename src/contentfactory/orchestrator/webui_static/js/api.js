// Máy khách HTTP: gắn token phiên, chuẩn hoá lỗi thành ApiError (có message/hint tiếng Việt để hiện cho người dùng, không bao giờ là stack trace).
const token = () => document.querySelector('meta[name="cf-token"]')?.content || "";

export class ApiError extends Error {
  constructor(message, { code = "ERROR", hint = "", status = 0, network = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code; this.hint = hint; this.status = status; this.network = network;
  }
}

async function request(method, path, { body, query, signal, raw } = {}) {
  let url = path;
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v != null && v !== "") qs.set(k, v);
    const s = qs.toString();
    if (s) url += (path.includes("?") ? "&" : "?") + s;
  }
  const headers = { "X-CF-Token": token(), Accept: "application/json" };
  let payload;
  if (raw !== undefined) { payload = raw; headers["Content-Type"] = "application/octet-stream"; }
  else if (body !== undefined) { payload = JSON.stringify(body); headers["Content-Type"] = "application/json"; }
  let res;
  try {
    res = await fetch(url, { method, headers, body: payload, signal, cache: "no-store" });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError("Không kết nối được tới ContentFactory. Ứng dụng có đang chạy không?", { network: true, hint: "Mở lại ContentFactory rồi tải lại trang." });
  }
  let data = null;
  try { data = await res.json(); } catch { /* không phải JSON */ }
  if (!res.ok) {
    const e = data?.error || {};
    throw new ApiError(e.message || `Lỗi ${res.status}`, { code: e.code || "HTTP_" + res.status, hint: e.hint || "", status: res.status });
  }
  return data;
}

// Ảnh/video xem trước cần token (không đặt header được trên <img>/<video>): tải thành blob rồi dùng object URL. Cache theo đường dẫn.
const blobs = new Map();
async function blobUrl(path, { fresh = false } = {}) {
  if (!fresh && blobs.has(path)) return blobs.get(path);
  let res;
  try { res = await fetch(path, { headers: { "X-CF-Token": token() }, cache: "no-store" }); }
  catch { throw new ApiError("Không kết nối được tới ContentFactory.", { network: true }); }
  if (!res.ok) {
    let e = {};
    try { e = (await res.json()).error || {}; } catch { /* không phải JSON */ }
    throw new ApiError(e.message || `Lỗi ${res.status}`, { code: e.code || "HTTP_" + res.status, hint: e.hint || "", status: res.status });
  }
  const url = URL.createObjectURL(await res.blob());
  blobs.set(path, url);
  return url;
}
export function forgetBlob(path) { const u = blobs.get(path); if (u) { URL.revokeObjectURL(u); blobs.delete(path); } }

export const api = {
  blobUrl,
  get: (path, opts) => request("GET", path, opts),
  post: (path, body, opts) => request("POST", path, { ...opts, body: body ?? {} }),
  put: (path, body, opts) => request("PUT", path, { ...opts, body: body ?? {} }),
  del: (path, opts) => request("DELETE", path, opts),
  upload: (path, bytes, opts) => request("PUT", path, { ...opts, raw: bytes }),
};

// request_id để backend chống tạo trùng khi bấm đúp/gửi lại (lớp thứ hai ngoài việc khóa nút).
export function newRequestId() {
  return (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
}

"""Worker TTS sống lâu cho adapters/worker_tts.py: nạp model MỘT lần rồi nhận yêu cầu JSON từng dòng qua stdin.

Chạy bằng python trong venv của chính engine (không import contentfactory):
    <venv>/Scripts/python.exe scripts/tts_worker.py vieneu|omnivoice
Giao thức (stdout chỉ dành cho giao thức, mọi log của engine dồn sang stderr):
    worker -> {"ready": true, "sample_rate": N}
    vào    -> {"text", "out", "voice", "language", "settings"}     (out: đường dẫn WAV cần ghi)
    ra     -> {"ok": true} | {"ok": false, "type": "<ExceptionClass>", "error": "..."}
Hết stdin (ContentFactory thoát) => worker thoát.
Tham số lúc nạp model qua biến môi trường: VIENEU_PRECISION (fp32|int8), VIENEU_THREADS, OMNIVOICE_MODEL, OMNIVOICE_DEVICE.
"""
import json
import os
import sys
import traceback


def _write(path, wav, sr):
    import numpy as np
    import soundfile as sf
    # format tường minh: đuôi .part khiến soundfile không tự đoán được định dạng
    sf.write(path, np.asarray(wav, dtype=np.float32).reshape(-1), sr, format="WAV", subtype="PCM_16")


def vieneu():
    from vieneu import Vieneu
    tts = Vieneu(mode="v3turbo", precision=os.environ.get("VIENEU_PRECISION", "fp32"),
                 threads=int(os.environ.get("VIENEU_THREADS", "6")))

    def synth(r):
        s = r.get("settings") or {}
        kw = {k: s[k] for k in ("temperature", "top_k", "top_p", "ref_audio") if s.get(k) is not None}
        _write(r["out"], tts.infer(r["text"], voice=r.get("voice"), **kw), tts.sample_rate)
    return synth, tts.sample_rate


def omnivoice():
    import torch
    from omnivoice.models.omnivoice import OmniVoice
    dev = os.environ.get("OMNIVOICE_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
    model = OmniVoice.from_pretrained(os.environ.get("OMNIVOICE_MODEL", "k2-fsa/OmniVoice"), device_map=dev,
                                      dtype=torch.float16 if dev == "cuda" else torch.float32)

    def synth(r):
        s = r.get("settings") or {}
        kw = {k: s[k] for k in ("speed", "num_step", "guidance_scale", "instruct") if s.get(k) is not None}
        a = model.generate(text=r["text"], language=r.get("language"), ref_audio=s.get("reference_audio"),
                           ref_text=s.get("reference_text"), **kw)
        _write(r["out"], a[0].float().cpu().numpy(), model.sampling_rate)
    return synth, model.sampling_rate


def main():
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)                       # thư viện engine (cả mã C) in ra fd 1 => dồn sang stderr
    sys.stdout = sys.stderr

    def send(obj):
        proto.write(json.dumps(obj, ensure_ascii=False) + "\n")

    synth, sr = {"vieneu": vieneu, "omnivoice": omnivoice}[sys.argv[1]]()
    send({"ready": True, "sample_rate": sr})
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        try:
            synth(json.loads(raw))
            send({"ok": True})
        except Exception as e:          # noqa: BLE001 — lỗi một segment không được giết worker
            traceback.print_exc()
            send({"ok": False, "type": type(e).__name__, "error": str(e)[-600:]})


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""EPIS tam baglam + Kairos proaktif VRAM testi (qwen3.5:9b)."""

import os
import sys
import time
import subprocess
import threading

# stdout buffering test ciktisini gizlemesin
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

EPIS_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(EPIS_ROOT, "Layer-2", "src")
sys.path.insert(0, SRC)

from memory_manager import MemoryManager
from context_builder import ContextBuilder
from epis_core import build_system_prompt, Layer1Engine
from proactive_delivery import build_kairos_user_message, PROACTIVE_SYSTEM


def vram_mib() -> tuple[int, int]:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free", "--format=csv,noheader,nounits"],
            text=True,
            timeout=5,
        ).strip()
        used, free = [int(x.strip()) for x in out.split(",")]
        return used, free
    except Exception:
        return -1, -1


class VramMonitor:
    def __init__(self):
        self.peak = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self.peak = vram_mib()[0]
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            used, _ = vram_mib()
            if used > self.peak:
                self.peak = used
            time.sleep(0.5)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        used, free = vram_mib()
        return used, free, self.peak


def _qwen_generate(engine, system_prompt: str, history: list) -> str:
    """Uzun timeout — 9B full context yavas olabilir."""
    from openai import OpenAI
    client = OpenAI(
        base_url=os.getenv("QWEN_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.getenv("QWEN_API_KEY", "not-needed"),
        timeout=600.0,
        max_retries=1,
    )
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        messages.append({"role": h["role"], "content": h["text"]})
    resp = client.chat.completions.create(
        model=engine.model,
        messages=messages,
        temperature=0.7,
        max_tokens=256,
    )
    msg = resp.choices[0].message
    text = (getattr(msg, "content", None) or "").strip()
    if not text:
        text = (getattr(msg, "reasoning", None) or "").strip()
    return text


def main():
    print("=== EPIS full context + Kairos VRAM testi ===\n", flush=True)

    idle_used, idle_free = vram_mib()
    print(f"Baslangic VRAM: {idle_used} MiB used / {idle_free} MiB free", flush=True)

    memory = MemoryManager()
    ctx = ContextBuilder(memory)
    engine = Layer1Engine(
        build_system_prompt(),
        context_provider=ctx.build,
    )

    engine.history = [
        {"role": "user", "text": "Selam EPIS, bugun nasil gidiyor?"},
        {"role": "assistant", "text": '{"type":"direct","message":"Iyiyim, sen nasilsin kullanıcı?"}'},
        {"role": "user", "text": "Biraz yoruldum, youtube izledim biraz."},
        {"role": "assistant", "text": '{"type":"direct","message":"Anladim. Ne izledin?"}'},
        {"role": "user", "text": "Bilim videolari, sonra reelse kaydım."},
        {"role": "assistant", "text": '{"type":"direct","message":"Tamam, dinlenmene bak."}'},
    ]

    user_msg = (
        "Şu an ekranlarımda neler var? Bugün YouTube ve Reels ne kadar sürdü? "
        "Nabız ve uyku durumum nasıl görünüyor?"
    )

    print("Baglam olusturuluyor (canli ekran + band + hafiza)...", flush=True)
    t_ctx = time.time()
    effective = engine._effective_system_prompt(user_msg)
    print(f"Baglam hazir: {time.time()-t_ctx:.1f}s", flush=True)

    prompt_chars = len(effective)
    approx_tokens = prompt_chars // 3
    print(f"\nModel: {engine.model}", flush=True)
    print(f"System+context: ~{prompt_chars:,} karakter (~{approx_tokens:,} token tahmini)", flush=True)
    print(f"History turu: {len(engine.history)} mesaj", flush=True)

    # --- 1) Tam context sohbet ---
    print("\n--- [1/2] Full context chat inference ---", flush=True)
    mon = VramMonitor()
    mon.start()
    t0 = time.time()
    try:
        raw = _qwen_generate(
            engine,
            effective,
            engine.history + [{"role": "user", "text": user_msg}],
        )
        err = None
    except Exception as e:
        raw = ""
        err = str(e)
    dt = time.time() - t0
    used, free, peak = mon.stop()

    preview = (raw[:200] + "...") if len(raw) > 200 else (raw or "(bos)")
    print(f"Sure: {dt:.1f}s", flush=True)
    print(f"VRAM peak: {peak} MiB ({peak/1024:.2f} GB)", flush=True)
    print(f"VRAM simdi: {used} MiB used / {free} MiB free", flush=True)
    if err:
        print(f"HATA: {err}", flush=True)
    else:
        print(f"Yanit onizleme: {preview}", flush=True)

    # --- 2) Kairos proaktif ---
    print("\n--- [2/2] Kairos proaktif (anomaly) ---", flush=True)
    kairos_ctx = "Kalp hizi son 15 dakikadır 92 bpm."
    mon2 = VramMonitor()
    mon2.start()
    t1 = time.time()
    try:
        situation = build_kairos_user_message("anomaly", kairos_ctx)
        user_k = (
            f"{situation}\n\n"
            "kullanıcıya tek mesaj yaz. Neden yazdigini dogal bir cumleyle soyle."
        )
        kairos_msg = _qwen_generate(engine, PROACTIVE_SYSTEM, [{"role": "user", "text": user_k}])
        kerr = None
    except Exception as e:
        kairos_msg = ""
        kerr = str(e)
    dt2 = time.time() - t1
    used2, free2, peak2 = mon2.stop()

    print(f"Sure: {dt2:.1f}s", flush=True)
    print(f"VRAM peak: {peak2} MiB ({peak2/1024:.2f} GB)", flush=True)
    print(f"VRAM simdi: {used2} MiB used / {free2} MiB free", flush=True)
    if kerr:
        print(f"HATA: {kerr}", flush=True)
    else:
        print(f"Kairos mesaji: {kairos_msg[:200]}", flush=True)

    overall_peak = max(peak, peak2)
    print("\n=== OZET ===", flush=True)
    print(f"Genel VRAM peak: {overall_peak} MiB ({overall_peak/1024:.2f} GB) / 8151 MiB toplam", flush=True)
    if overall_peak >= 8100:
        print("UYARI: VRAM limitine cok yakin — OOM riski yuksek", flush=True)
    elif overall_peak >= 7900:
        print("NOT: Sigiyor ama dar — oyun/Chrome ile birlikte zorlanir", flush=True)
    else:
        print("OK: Full context + Kairos 8GB icinde", flush=True)


if __name__ == "__main__":
    main()

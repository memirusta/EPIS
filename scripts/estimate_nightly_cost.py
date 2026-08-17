#!/usr/bin/env python3
"""
Nightly maliyet tahmini — resmi fiyatlar (Temmuz 2026):
  Gemini 3.1 Pro Preview: $2 / $12 per 1M (ai.google.dev)
  Claude Opus 4.8:       $5 / $25 per 1M (docs.anthropic.com)
Opus 4.7+ tokenizer ~%30 daha fazla token sayar (aynı metin).
"""
USD_TL = 46.67
GEMINI_IN, GEMINI_OUT = 2.0, 12.0
CLAUDE_IN, CLAUDE_OUT = 5.0, 25.0
OPUS_TOKENIZER = 1.30  # Anthropic resmi notu


def cost(in_t, out_t, pin, pout):
    return (in_t / 1e6) * pin + (out_t / 1e6) * pout


def claude_tokens(n):
    return int(n * OPUS_TOKENIZER)


scenarios = {
    "bugun (3 oturum)": dict(
        s1_in=4500, s1_out=1800, s1_turns=2,
        s2_in=3200, s2_out=700, s2_turns=1,
        drift_in=2200, drift_out=120,
    ),
    "normal gun": dict(
        s1_in=6500, s1_out=2200, s1_turns=2,
        s2_in=3800, s2_out=900, s2_turns=2,
        drift_in=2500, drift_out=150,
    ),
    "yogun + tokenizer + 3+3 tur": dict(
        s1_in=10000, s1_out=3500, s1_turns=3,
        s2_in=8500, s2_out=1800, s2_turns=3,
        drift_in=2800, drift_out=200,
    ),
    "kotu senaryo (her API 3 retry)": dict(
        s1_in=10000 * 3, s1_out=3500 * 3, s1_turns=3,
        s2_in=8500 * 3, s2_out=1800 * 3, s2_turns=3,
        drift_in=2800 * 3, drift_out=200 * 3,
    ),
}

for name, s in scenarios.items():
    g = cost(s["s1_in"], s["s1_out"], GEMINI_IN, GEMINI_OUT)
    c2_in, c2_out = claude_tokens(s["s2_in"]), claude_tokens(s["s2_out"])
    d_in, d_out = claude_tokens(s["drift_in"]), claude_tokens(s["drift_out"])
    c = cost(c2_in, c2_out, CLAUDE_IN, CLAUDE_OUT)
    d = cost(d_in, d_out, CLAUDE_IN, CLAUDE_OUT)
    tot = g + c + d
    print(f"=== {name} ===")
    print(f"  Gemini:  ${g:.4f}")
    print(f"  Claude:  ${c+d:.4f} (tokenizer x{OPUS_TOKENIZER})")
    print(f"  TOPLAM:  ${tot:.4f} USD  |  {tot*USD_TL:.2f} TL")
    print()

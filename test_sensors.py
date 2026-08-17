#!/usr/bin/env python3
"""Gadgetbridge sensör testi -- API gerekmez."""
import os
import sys
from dotenv import load_dotenv

EPIS_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(EPIS_ROOT, "Layer-2", "src"))
load_dotenv(os.path.join(EPIS_ROOT, "Layer-3", "keys.env"))

from sensors import GadgetbridgeReader

def main():
    gb = GadgetbridgeReader()
    print("=" * 50)
    print("EPIS Gadgetbridge Test")
    print("=" * 50)
    print(f"DB yolu : {gb.db_path}")
    print(f"DB var  : {gb.is_available()}")

    fresh = gb.get_db_freshness()
    if fresh.get("available"):
        print(f"DB yasi : {fresh['age_minutes']} dk (son export: {fresh['modified_at']})")
        if fresh.get("stale_warning"):
            print("  UYARI: 20+ dk eski -- Tasker ile export sikligini artir (asagiya bak).")

    if not gb.is_available():
        print("\nHATA: DB bulunamadi. keys.env -> GADGETBRIDGE_DB_PATH kontrol et.")
        return

    gb.refresh_schema()
    print(f"Tablo   : {gb._table}")
    print(f"Sutunlar: {sorted(gb._cols)}")

    cov = gb.get_hr_coverage(hours=18)
    if cov.get("available"):
        print(f"\nNabiz kapsama (gece): %{cov['coverage_pct']} ({cov['valid_hr']}/{cov['samples']})")
        print(f"Mod: {'nabiz oncelikli' if cov.get('hr_primary') else 'nabiz+hareket yedek'}")

    print("\n--- Uyku durumu (son 10 dk) ---")
    state = gb.get_current_sleep_state()
    print(f"Durum: {state or 'veri yok / uyanik'}")

    print("\n--- Son gece uyku baslangici (nabiz + adim) ---")
    onset = gb.get_sleep_onset_time(lookback_hours=18)
    print(f"Yontem: nabiz oncelikli (surekli HR aciksa intensity yedegi kapali)")
    print(f"Uyku baslangici: {onset or 'tespit edilemedi'}")

    wake = gb.get_wake_time(lookback_hours=18)
    print("\n--- Uyanma saati ---")
    print(f"Uyanma: {wake or 'tespit edilemedi'}")
    if onset and wake:
        mins = int((wake - onset).total_seconds() / 60)
        print(f"Uyku suresi (baslangic->uyanma): ~{mins} dk ({mins/60:.1f} saat)")

    print("\n--- Nabiz (son 1 saat) ---")
    hr = gb.get_heart_rate_summary(hours=1)
    if hr.get("available"):
        print(f"Ort: {hr['avg']} bpm | Min: {hr['min']} | Max: {hr['max']} | Ornek: {hr['samples']}")
    else:
        print("Nabiz verisi yok")

    print("\n--- Stres anomalisi (esik 130 bpm) ---")
    print(f"Anomali: {gb.is_stress_anomaly(threshold_bpm=130)}")

    print("\n--- Bugunun ozeti ---")
    summary = gb.get_today_summary()
    for k, v in summary.items():
        print(f"  {k}: {v}")

    print("\n" + "=" * 50)
    print("Tamam. Yukaridaki degerler mantikliysa sensörler calisiyor.")
    print("=" * 50)

if __name__ == "__main__":
    main()

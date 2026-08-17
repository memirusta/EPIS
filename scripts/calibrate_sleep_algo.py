#!/usr/bin/env python3
"""Uyku algoritmasi adaylarini Gadgetbridge verisi uzerinde karsilastirir."""
import os
import sqlite3
from datetime import datetime, timedelta

db = os.path.join(os.path.dirname(__file__), "..", "GadgetBridge", "Gadgetbridge.db")
INVALID = {0, 255}
SLEEP_HR = (40, 72)
AWAKE_HR = 78
SLEEP_KINDS = {224, 235, 240, 249, 250}


def fetch(since, until):
    c = sqlite3.connect(db)
    cur = c.cursor()
    cur.execute(
        "SELECT TIMESTAMP, HEART_RATE, STEPS, RAW_INTENSITY, RAW_KIND "
        "FROM MI_BAND_ACTIVITY_SAMPLE WHERE TIMESTAMP BETWEEN ? AND ? ORDER BY TIMESTAMP",
        (since, until),
    )
    rows = [(ts, hr, st or 0, inten or 0, kind) for ts, hr, st, inten, kind in cur.fetchall()]
    c.close()
    return rows


def is_sleep_v2(hr, steps, inten, kind, night=True):
    if steps > 0:
        return False
    if kind == 112 and hr not in INVALID and hr > 72:
        return False
    if hr not in INVALID:
        if hr < SLEEP_HR[0]:
            return False
        if hr <= SLEEP_HR[1]:
            return True
        if hr <= AWAKE_HR:
            return inten <= 20
        return False
    if night and inten <= 15 and kind not in {80, 96, 1}:
        return True
    if kind in SLEEP_KINDS:
        return True
    return inten <= 5


def is_wake_v2(hr, steps, inten, kind):
    if steps > 0:
        return True
    if hr not in INVALID and hr >= 75:
        return True
    if hr not in INVALID and hr >= 68 and inten >= 35:
        return True
    if kind == 112 and hr not in INVALID and hr >= 70 and inten >= 25:
        return True
    return False


def longest_block(rows, fn, min_m=180):
    best = None
    cur = None
    for ts, hr, st, inten, kind in rows:
        ok = fn(ts, hr, st, inten, kind)
        if ok:
            if cur is None:
                cur = [ts, ts]
            else:
                cur[1] = ts
        else:
            if cur and (cur[1] - cur[0]) // 60 >= min_m:
                if best is None or cur[1] - cur[0] > best[1] - best[0]:
                    best = cur[:]
            cur = None
    if cur and (cur[1] - cur[0]) // 60 >= min_m:
        if best is None or cur[1] - cur[0] > best[1] - best[0]:
            best = cur
    return best


def wake_forward(rows, min_awake=8):
    """Uyku blogundan sonra ilk kalici uyanik pencere."""
    block = longest_block(rows, lambda ts, hr, st, i, k: is_sleep_v2(hr, st, i, k))
    if not block:
        return None, None
    after = [r for r in rows if r[0] > block[1]]
    need = min_awake
    for i in range(len(after) - need + 1):
        w = after[i : i + need]
        t0 = datetime.fromtimestamp(w[0][0])
        if t0.hour < 6:
            continue
        awake = sum(1 for ts, hr, st, inten, kind in w if is_wake_v2(hr, st, inten, kind))
        if awake >= need - 1:
            return block, w[0][0]
    return block, block[1]


since = int(datetime(2026, 7, 1, 20, 0).timestamp())
until = int(datetime(2026, 7, 2, 12, 0).timestamp())
rows = fetch(since, until)

block, wake_ts = wake_forward(rows)
if block:
    onset = datetime.fromtimestamp(block[0])
    wake = datetime.fromtimestamp(wake_ts)
    mins = (wake_ts - block[0]) // 60
    print(f"v2 longest+forward: {onset:%H:%M} - {wake:%H:%M} = {mins} dk ({mins/60:.2f} saat)")
    print(f"  GB hedef: 00:11 - 09:44 = 585 dk (9s45)")

"""
EPIS -- Sensor Katmani
======================
Mi Band 6 (Gadgetbridge SQLite) + Windows ekran izleme.

Gadgetbridge kurulumu:
  1. Telefona Gadgetbridge yukle
  2. Mi Band 6 bagla
  3. Gadgetbridge -> Ayarlar -> Veritabani Klasoru -> export yolunu ayarla
  4. GADGETBRIDGE_DB_PATH env degiskenini keys.env'e yaz
"""

import os
import json
import time
import sqlite3
import logging
from datetime import datetime, timedelta, date
from typing import Optional

logger = logging.getLogger(__name__)

EPIS_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SCREEN_DAILY_PATH = os.path.join(EPIS_ROOT, "Layer-1", "memory", "screen_daily.json")

# Uyku tespiti: HEART_RATE oncelikli (Gadgetbridge: surekli nabiz / uyku icin nabiz acik)
# RAW_KIND (240, 80, 112...) kullanilmaz.
AWAKE_KIND = 112
SAMPLE_INTERVAL_MINUTES = 1
MIN_SLEEP_ONSET_MINUTES = 10
ONSET_MIN_HOUR = 0
ONSET_MIN_MINUTE = 10
ONSET_HR_MIN_SAMPLES = 5
ONSET_HR_MIN_SAMPLES_ACTIVE = 4   # surekli nabiz acikken
WAKE_AWAKE_MINUTES = 8
WAKE_HR_MIN = 75                  # kalici uyanma: nabiz esigi
WAKE_HR_MID = 68                  # nabiz + hareket birlikte
WAKE_INTENSITY_MIN = 35
HR_PRIMARY_COVERAGE = 0.35          # gece verisinin bu kadari gecerli nabizsa nabiz modu
SLEEP_HR_MIN = 40
SLEEP_HR_MAX = 72
SLEEP_HR_AWAKE = 78                 # ustunde = uyanik (hr modunda)
SLEEP_HR_GAP_INTENSITY_MAX = 12   # nabiz yokken (255) dusuk hareket = uyku
SLEEP_HR_ONSET_AVG_MAX = 70
INVALID_HR = frozenset({0, 255})
SLEEP_INTENSITY_MAX = 5               # yalnizca nabiz yokken (yedek)
SLEEP_ONSET_INTENSITY_AVG_MAX = 1


class GadgetbridgeReader:

    # Mi Band 6 (Huami) icin olasi aktivite tablolari -- oncelik sirasi.
    # Otomatik tespit yine de COUNT'a gore en dolu olani secer; bu liste
    # yalnizca esit durumda tercih ve aday filtresi icindir.
    _PREFERRED_TABLES = [
        "HUAMI_EXTENDED_ACTIVITY_SAMPLE",
        "MI_BAND_ACTIVITY_SAMPLE",
        "XIAOMI_ACTIVITY_SAMPLE",
    ]

    def __init__(self, db_path: str = None):
        self.db_path = (
            db_path
            or os.getenv("GADGETBRIDGE_DB_PATH")
            or self._default_path()
        )
        # Tespit sonuclari (tembel + onbellekli)
        self._table: Optional[str]      = None
        self._cols:  set                = set()
        self._last_detect: Optional[datetime] = None
        self._redetect_interval         = timedelta(minutes=5)

    def _default_path(self) -> str:
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "Layer-3", "gadgetbridge.db"
        )

    def is_available(self) -> bool:
        return os.path.exists(self.db_path)

    def get_db_freshness(self) -> dict:
        """Syncthing/export sonrasi DB ne kadar eski? (Gadgetbridge min 1 saat export eder.)"""
        if not self.is_available():
            return {"available": False}
        mtime = os.path.getmtime(self.db_path)
        age_m = (time.time() - mtime) / 60
        return {
            "available":       True,
            "modified_at":     datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
            "age_minutes":     round(age_m, 1),
            "stale_warning":   age_m > 20,
        }

    def _connect(self):
        if not self.is_available():
            raise FileNotFoundError(
                f"Gadgetbridge DB bulunamadi: {self.db_path}\n"
                "GADGETBRIDGE_DB_PATH env degiskenini kontrol et."
            )
        return sqlite3.connect(self.db_path)

    # ------------------------------------------------------------------
    # Otomatik tablo/sutun tespiti
    # ------------------------------------------------------------------

    def _table_columns(self, cursor, table: str) -> set:
        try:
            cursor.execute(f"PRAGMA table_info({table})")
            return {row[1].upper() for row in cursor.fetchall()}
        except Exception:
            return set()

    def _detect_schema(self, force: bool = False):
        """
        Hangi *_ACTIVITY_SAMPLE tablosunda veri varsa onu secer.
        Veri yoksa _table None kalir; sorgular zarifce bos doner.
        Tablo bulunduysa bir daha taramaz. Bulunamadiysa _redetect_interval
        araliklarinda yeniden dener (veri sonradan gelebilir).
        """
        if not force:
            if self._table is not None:
                return
            if self._last_detect and (datetime.now() - self._last_detect) < self._redetect_interval:
                return
        self._last_detect = datetime.now()
        self._table, self._cols = None, set()

        if not self.is_available():
            return
        try:
            conn   = self._connect()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE '%ACTIVITY_SAMPLE'"
            )
            candidates = [r[0] for r in cursor.fetchall()]

            def pref_rank(name: str) -> int:
                return self._PREFERRED_TABLES.index(name) if name in self._PREFERRED_TABLES else 99

            best, best_cols, best_count = None, set(), 0
            for t in candidates:
                cols = self._table_columns(cursor, t)
                if "TIMESTAMP" not in cols:
                    continue
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {t}")
                    cnt = cursor.fetchone()[0]
                except Exception:
                    continue
                if cnt == 0:
                    continue
                # Once satir sayisi, esitlikte tercih listesi onceligi (kucuk rank = oncelikli)
                if (cnt > best_count) or (cnt == best_count and pref_rank(t) < pref_rank(best or "")):
                    best, best_cols, best_count = t, cols, cnt

            conn.close()

            if best and best_count > 0:
                self._table, self._cols = best, best_cols
                logger.info(f"[Gadgetbridge] Aktif tablo: {best} ({best_count} satir, sutunlar: {sorted(best_cols)})")
            else:
                logger.info("[Gadgetbridge] Dolu aktivite tablosu yok -- veri bekleniyor.")
        except Exception as e:
            logger.warning(f"[Gadgetbridge] Sema tespiti basarisiz: {e}")

    def _ready(self) -> bool:
        """Tablo tespit edildiyse True. Tespiti tetikler."""
        self._detect_schema()
        return self._table is not None

    def _has(self, *columns) -> bool:
        return all(c.upper() in self._cols for c in columns)

    def refresh_schema(self):
        """Yeni veri/yeni DB sonrasi tabloyu yeniden tespit etmek icin."""
        self._detect_schema(force=True)

    @staticmethod
    def _is_night_hour(ts: int) -> bool:
        """Gece penceresi: 20:00 - 12:00 (ogle uykusunu ayirmak icin)."""
        h = datetime.fromtimestamp(ts).hour
        return h >= 20 or h < 12

    @staticmethod
    def _valid_hr(heart_rate: Optional[int]) -> bool:
        if heart_rate is None or heart_rate in INVALID_HR:
            return False
        return SLEEP_HR_MIN <= heart_rate <= SLEEP_HR_MAX

    @staticmethod
    def _hr_coverage(rows: list) -> float:
        if not rows:
            return 0.0
        valid = sum(1 for _, _, hr, _, _ in rows if GadgetbridgeReader._valid_hr(hr))
        return valid / len(rows)

    def _hr_primary_mode(self, rows: list) -> bool:
        """Surekli nabiz aciksa gece verisinde yuksek HR orani; intensity yedegi kapanir."""
        return self._hr_coverage(rows) >= HR_PRIMARY_COVERAGE

    @staticmethod
    def _is_sleep_minute(
        heart_rate: Optional[int],
        steps: Optional[int],
        kind: Optional[int] = None,
        intensity: Optional[int] = None,
        *,
        hr_primary: bool = False,
    ) -> bool:
        """
        Uyku dakikasi:
          hr_primary: sadece gecerli dusuk nabiz + sifir adim (surekli nabiz modu)
          aksi halde: nabiz varsa nabiz, yoksa dusuk hareket yogunlugu (yedek)
        """
        if kind == AWAKE_KIND:
            return False
        if steps is not None and steps > 0:
            return False

        if heart_rate is not None and heart_rate not in INVALID_HR:
            hr_cap = SLEEP_HR_AWAKE if hr_primary else SLEEP_HR_MAX
            if heart_rate > hr_cap:
                return False
            if SLEEP_HR_MIN <= heart_rate <= hr_cap:
                if heart_rate > SLEEP_HR_MAX and intensity is not None and intensity > 20:
                    return False
                return True
            return False

        if intensity is not None and intensity <= (
            SLEEP_HR_GAP_INTENSITY_MAX if hr_primary else SLEEP_INTENSITY_MAX
        ):
            return True
        return False

    @staticmethod
    def _is_wake_minute(
        heart_rate: Optional[int],
        steps: Optional[int],
        intensity: Optional[int] = None,
    ) -> bool:
        """
        Kalici uyanma dakikasi. RAW_KIND=112 tek basina yeterli degil
        (uyku icinde kisa 112 bloklari GB'yi yaniltiyordu).
        """
        if steps is not None and steps > 0:
            return True
        if heart_rate is None or heart_rate in INVALID_HR:
            return False
        if heart_rate >= WAKE_HR_MIN:
            return True
        if heart_rate >= WAKE_HR_MID and intensity is not None and intensity >= WAKE_INTENSITY_MIN:
            return True
        return False

    def _sleep_window_ok(
        self,
        window: list,
        *,
        for_onset: bool = False,
        hr_primary: bool = False,
    ) -> bool:
        """Ardisik pencere gercek uyku blogu mu?"""
        if not window:
            return False
        n = len(window)
        use_hr = hr_primary or self._hr_primary_mode(window)
        if any(st and st > 0 for _, _, _, st, _ in window):
            return False
        if sum(1 for _, k, _, _, _ in window if k == AWAKE_KIND) >= 2:
            return False

        sleep_n = sum(
            1 for _, k, hr, st, intensity in window
            if self._is_sleep_minute(hr, st, k, intensity, hr_primary=use_hr)
        )
        if sleep_n < int(n * 0.8):
            return False

        hrs = [hr for _, _, hr, _, _ in window if self._valid_hr(hr)]

        if for_onset:
            if not self._onset_start_allowed(window[0][0]):
                return False
            low_hrs = [hr for hr in hrs if hr <= SLEEP_HR_ONSET_AVG_MAX]
            need_hr = ONSET_HR_MIN_SAMPLES_ACTIVE if use_hr else ONSET_HR_MIN_SAMPLES
            if len(low_hrs) >= need_hr:
                return (sum(low_hrs) / len(low_hrs)) <= SLEEP_HR_ONSET_AVG_MAX
            t0 = datetime.fromtimestamp(window[0][0])
            if t0.hour < 12:
                gap_sleep = sum(
                    1 for _, k, hr, st, intensity in window
                    if hr in INVALID_HR
                    and (intensity is None or intensity <= SLEEP_HR_GAP_INTENSITY_MAX)
                    and not (st and st > 0)
                )
                if sleep_n >= int(n * 0.8) and gap_sleep >= int(n * 0.5):
                    return True
            return False

        if use_hr and len(hrs) >= int(n * 0.5):
            return (sum(hrs) / len(hrs)) <= SLEEP_HR_ONSET_AVG_MAX

        if len(hrs) >= int(n * 0.5):
            return (sum(hrs) / len(hrs)) <= SLEEP_HR_ONSET_AVG_MAX

        ints = [i for _, _, _, _, i in window if i is not None]
        if not ints:
            return False
        return (
            (sum(ints) / len(ints)) <= SLEEP_ONSET_INTENSITY_AVG_MAX
            and max(ints) <= SLEEP_INTENSITY_MAX
        )

    @staticmethod
    def _onset_start_allowed(ts: int) -> bool:
        """Ana gece uykusu: 22:00+ veya 00:10 sonrasi (aksam kisa dinlenme atlanir)."""
        t = datetime.fromtimestamp(ts)
        h, m = t.hour, t.minute
        if h >= 22:
            return True
        if h < 12 and (h > 0 or m >= ONSET_MIN_MINUTE):
            return True
        return False

    def _fetch_activity_rows(self, since_ts: int) -> list:
        """(TIMESTAMP, RAW_KIND, HEART_RATE, STEPS, RAW_INTENSITY) eskiden yeniye."""
        conn   = self._connect()
        cursor = conn.cursor()
        cols   = self._cols
        parts  = ["TIMESTAMP"]
        parts.append("RAW_KIND" if "RAW_KIND" in cols else "NULL AS RAW_KIND")
        parts.append("HEART_RATE" if "HEART_RATE" in cols else "NULL AS HEART_RATE")
        parts.append("STEPS" if "STEPS" in cols else "NULL AS STEPS")
        parts.append("RAW_INTENSITY" if "RAW_INTENSITY" in cols else "NULL AS RAW_INTENSITY")
        cursor.execute(
            f"SELECT {', '.join(parts)} FROM {self._table} "
            "WHERE TIMESTAMP >= ? ORDER BY TIMESTAMP ASC",
            (since_ts,),
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

    def _try_huami_sleep_session_onset(self) -> Optional[datetime]:
        """Gadgetbridge islenmis uyku oturumu varsa onu kullan (en guvenilir)."""
        try:
            conn = self._connect()
            cur  = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='HUAMI_SLEEP_SESSION_SAMPLE'"
            )
            if not cur.fetchone():
                conn.close()
                return None
            cur.execute("SELECT COUNT(*) FROM HUAMI_SLEEP_SESSION_SAMPLE")
            if cur.fetchone()[0] == 0:
                conn.close()
                return None
            cur.execute("PRAGMA table_info(HUAMI_SLEEP_SESSION_SAMPLE)")
            col_names = [r[1] for r in cur.fetchall()]
            upper     = {c.upper(): c for c in col_names}
            start_col = None
            for key in ("TIMESTAMP_FROM", "TIMESTAMP_START", "START_TIME", "TIMESTAMP"):
                if key in upper:
                    start_col = upper[key]
                    break
            if not start_col:
                conn.close()
                return None
            cur.execute(
                f"SELECT {start_col} FROM HUAMI_SLEEP_SESSION_SAMPLE "
                f"ORDER BY {start_col} DESC LIMIT 1"
            )
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                ts = row[0]
                if ts > 1_000_000_000_000:
                    ts = ts // 1000
                return datetime.fromtimestamp(ts)
        except Exception as e:
            logger.debug(f"[Gadgetbridge] HUAMI_SLEEP_SESSION: {e}")
        return None

    @staticmethod
    def _ts_to_datetime(ts) -> Optional[datetime]:
        if ts is None:
            return None
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            return None
        if ts > 1_000_000_000_000:
            ts = ts // 1000
        return datetime.fromtimestamp(ts)

    def _sleep_tables_empty(self) -> bool:
        try:
            conn = self._connect()
            cur  = conn.cursor()
            for table in (
                "HUAMI_SLEEP_SESSION_SAMPLE",
                "XIAOMI_SLEEP_TIME_SAMPLE",
            ):
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                if cur.fetchone():
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    if cur.fetchone()[0] > 0:
                        conn.close()
                        return False
            conn.close()
            return True
        except Exception:
            return True

    def _get_xiaomi_sleep_session(self) -> Optional[dict]:
        """Gadgetbridge islenmis uyku (telefon uygulamasindaki kart verisi)."""
        try:
            conn = self._connect()
            cur  = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='XIAOMI_SLEEP_TIME_SAMPLE'"
            )
            if not cur.fetchone():
                conn.close()
                return None
            cur.execute("SELECT COUNT(*) FROM XIAOMI_SLEEP_TIME_SAMPLE")
            if cur.fetchone()[0] == 0:
                conn.close()
                return None
            cur.execute(
                "SELECT TIMESTAMP, WAKEUP_TIME, TOTAL_DURATION, DEEP_SLEEP_DURATION, "
                "LIGHT_SLEEP_DURATION FROM XIAOMI_SLEEP_TIME_SAMPLE "
                "ORDER BY WAKEUP_TIME DESC LIMIT 1"
            )
            row = cur.fetchone()
            conn.close()
            if not row:
                return None
            _ts, wake_raw, total_dur, _deep, _light = row
            wake = self._ts_to_datetime(wake_raw)
            if not wake or not total_dur:
                return None
            total_min = int(total_dur // 60) if total_dur > 300 else int(total_dur)
            onset = wake - timedelta(minutes=total_min)
            return {
                "sleep_onset":      onset,
                "wake_time":        wake,
                "sleep_minutes":    total_min,
                "sleep_detection":  "xiaomi_sleep_table",
            }
        except Exception as e:
            logger.debug(f"[Gadgetbridge] XIAOMI_SLEEP_TIME: {e}")
            return None

    def _get_last_activity_timestamp(self) -> Optional[datetime]:
        if not self._ready():
            return None
        try:
            conn = self._connect()
            cur  = conn.cursor()
            cur.execute(f"SELECT MAX(TIMESTAMP) FROM {self._table}")
            row = cur.fetchone()
            conn.close()
            return self._ts_to_datetime(row[0] if row else None)
        except Exception:
            return None

    def _get_processed_sleep(self) -> Optional[dict]:
        """Oncelik: Xiaomi/Huami islenmis uyku tablosu."""
        x = self._get_xiaomi_sleep_session()
        if x:
            return x
        onset = self._try_huami_sleep_session_onset()
        if onset:
            return {
                "sleep_onset":      onset,
                "wake_time":        None,
                "sleep_minutes":    None,
                "sleep_detection":  "huami_sleep_session",
            }
        return None


    def get_hr_coverage(self, hours: float = 12) -> dict:
        """Surekli nabiz acik mi? (gece verisinde gecerli HEART_RATE orani)"""
        if not self._ready() or not self._has("HEART_RATE"):
            return {"available": False}
        cutoff = int((datetime.now() - timedelta(hours=hours)).timestamp())
        rows = [
            r for r in self._fetch_activity_rows(cutoff)
            if self._is_night_hour(r[0])
        ]
        if not rows:
            return {"available": False}
        cov = self._hr_coverage(rows)
        return {
            "available":    True,
            "coverage_pct": round(cov * 100, 1),
            "hr_primary":   cov >= HR_PRIMARY_COVERAGE,
            "samples":      len(rows),
            "valid_hr":     sum(1 for _, _, hr, _, _ in rows if self._valid_hr(hr)),
        }

    def get_current_sleep_state(self) -> Optional[str]:
        if not self._ready() or not self._has("HEART_RATE"):
            return None
        try:
            cutoff = int((datetime.now() - timedelta(minutes=10)).timestamp())
            rows   = self._fetch_activity_rows(cutoff)
            if not rows:
                return None
            hr_pri = self._hr_primary_mode(rows)
            recent = rows[-5:]
            sleep_count = sum(
                1 for ts, k, hr, st, intensity in recent
                if self._is_sleep_minute(hr, st, k, intensity, hr_primary=hr_pri)
            )
            return "sleeping" if sleep_count >= 3 else "awake"
        except Exception as e:
            logger.warning(f"[Gadgetbridge] Uyku durumu okunamadi: {e}")
            return None

    def get_sleep_onset_time(self, lookback_hours: int = 12) -> Optional[datetime]:
        """
        Gecenin gercek uyku baslangici (nabiz + adim; RAW_KIND kullanilmaz).
        Once HUAMI_SLEEP_SESSION varsa onu kullanir.
        """
        if not self._ready() or not self._has("HEART_RATE"):
            return None
        try:
            huami = self._try_huami_sleep_session_onset()
            if huami:
                return huami

            cutoff = int((datetime.now() - timedelta(hours=lookback_hours)).timestamp())
            rows   = [
                r for r in self._fetch_activity_rows(cutoff)
                if self._is_night_hour(r[0])
            ]
            need = MIN_SLEEP_ONSET_MINUTES
            if len(rows) < need:
                return None

            hr_pri = self._hr_primary_mode(rows)
            for i in range(len(rows) - need + 1):
                window = rows[i : i + need]
                if self._sleep_window_ok(window, for_onset=True, hr_primary=hr_pri):
                    return datetime.fromtimestamp(window[0][0])
            return None
        except Exception as e:
            logger.warning(f"[Gadgetbridge] Uyku baslangici okunamadi: {e}")
            return None

    def get_wake_time(self, lookback_hours: int = 18) -> Optional[datetime]:
        """Uyku baslangicindan sonra ilk kalici uyanik blok."""
        if not self._ready():
            return None
        try:
            onset = self.get_sleep_onset_time(lookback_hours=lookback_hours)
            if not onset:
                return None
            onset_ts = int(onset.timestamp())
            min_wake_ts = onset_ts + 3 * 3600
            rows = [
                r for r in self._fetch_activity_rows(onset_ts)
                if r[0] > min_wake_ts
            ]
            need = WAKE_AWAKE_MINUTES
            if len(rows) < need:
                return None
            need_awake = need - 1
            for i in range(len(rows) - need + 1):
                window = rows[i : i + need]
                t0 = datetime.fromtimestamp(window[0][0])
                if t0.hour < 8:
                    continue
                awake_n = sum(
                    1 for _, k, hr, st, intensity in window
                    if self._is_wake_minute(hr, st, intensity)
                )
                if awake_n >= need_awake:
                    return t0
            return None
        except Exception as e:
            logger.warning(f"[Gadgetbridge] Uyanma saati okunamadi: {e}")
            return None

    def get_heart_rate_summary(self, hours: float = 1) -> dict:
        if not self._ready() or not self._has("HEART_RATE"):
            return {"available": False}
        try:
            conn   = self._connect()
            cursor = conn.cursor()
            cutoff = int((datetime.now() - timedelta(hours=hours)).timestamp())
            cursor.execute(
                f"SELECT HEART_RATE FROM {self._table} "
                "WHERE TIMESTAMP >= ? AND HEART_RATE > 0 AND HEART_RATE NOT IN (255) "
                "AND HEART_RATE < 220",
                (cutoff,)
            )
            rates = [r[0] for r in cursor.fetchall()]
            conn.close()

            if not rates:
                return {"available": False}
            return {
                "available": True,
                "avg":       round(sum(rates) / len(rates)),
                "min":       min(rates),
                "max":       max(rates),
                "samples":   len(rates),
            }
        except Exception as e:
            logger.warning(f"[Gadgetbridge] Kalp hizi okunamadi: {e}")
            return {"available": False}

    def is_stress_anomaly(self, threshold_bpm: int = 100) -> bool:
        summary = self.get_heart_rate_summary(hours=0.25)
        if not summary.get("available"):
            return False
        return summary.get("avg", 0) > threshold_bpm

    def get_today_summary(self) -> dict:
        if not self._ready():
            return {"available": False}
        has_hr = self._has("HEART_RATE")
        has_st = self._has("STEPS")
        has_kind = self._has("RAW_KIND")
        try:
            today  = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            cutoff = int(today.timestamp())
            rows   = self._fetch_activity_rows(cutoff)
            night  = [r for r in rows if self._is_night_hour(r[0])]
            hr_pri = self._hr_primary_mode(night) if night else False
            hr_cov = self.get_hr_coverage(hours=18)

            processed = self._get_processed_sleep()
            if processed:
                onset = processed.get("sleep_onset")
                wake  = processed.get("wake_time")
                sleep_minutes = processed.get("sleep_minutes")
                mode = processed.get("sleep_detection", "processed_table")
            else:
                onset = self.get_sleep_onset_time(lookback_hours=18)
                wake  = self.get_wake_time(lookback_hours=18)
                sleep_minutes = None
                mode = "hr_heuristic"

            if sleep_minutes is None and has_hr and onset and wake and wake > onset:
                span = int((wake - onset).total_seconds() / 60)
                sleep_minutes = max(span, 0)
            elif sleep_minutes is None and has_hr:
                sleep_minutes = sum(
                    SAMPLE_INTERVAL_MINUTES
                    for ts, k, hr, st, intensity in rows
                    if self._is_night_hour(ts)
                    and self._is_sleep_minute(hr, st, k, intensity, hr_primary=hr_pri)
                )
            active_minutes = sum(
                SAMPLE_INTERVAL_MINUTES
                for ts, k, _, _, _ in rows
                if has_kind and k == AWAKE_KIND
            ) if has_kind else None
            hr_values = [
                hr for _, _, hr, _, _ in rows
                if hr and hr not in INVALID_HR and 0 < hr < 220
            ] if has_hr else []
            avg_hr    = round(sum(hr_values) / len(hr_values)) if hr_values else None

            if mode == "hr_heuristic":
                mode = "hr_primary" if hr_pri else "hr_intensity_hybrid"

            last_act = self._get_last_activity_timestamp()
            sleep_warn = None
            if self._sleep_tables_empty():
                fresh = self.get_db_freshness()
                age = fresh.get("age_minutes")
                sleep_warn = (
                    "Uyku oturumu tablosu PC veritabaninda bos; telefon uygulamasindaki "
                    "uyku (ornegin 10s) henuz export/senkron olmamis olabilir. "
                    "Asagidaki uyku tahmini nabiz/heuristic — yanlis geceyi gosterebilir."
                )
                if last_act and age is not None:
                    sleep_warn += f" Son aktivite ornegi: {last_act.strftime('%d.%m %H:%M')}."

            return {
                "available":        True,
                "date":             today.date().isoformat(),
                "table":            self._table,
                "sleep_detection":  mode,
                "sleep_data_warning": sleep_warn,
                "last_activity_at": last_act.isoformat(timespec="minutes") if last_act else None,
                "hr_coverage_pct":  hr_cov.get("coverage_pct") if hr_cov.get("available") else None,
                "sleep_minutes":    sleep_minutes,
                "sleep_onset":      onset.isoformat(timespec="minutes") if onset else None,
                "wake_time":        wake.isoformat(timespec="minutes") if wake else None,
                "active_minutes":   active_minutes,
                "avg_heart_rate":   avg_hr,
                "sample_count":     len(rows),
                "has_steps_column": has_st,
            }
        except Exception as e:
            logger.warning(f"[Gadgetbridge] Gunluk ozet okunamadi: {e}")
            return {"available": False}


class ScreenMonitor:
    """
    Aktif pencere basligini ve sureci izler.
    Ekran goruntusu veya icerik okuma YAPILMAZ — sadece metadata.
    Gunluk kullanim screen_daily.json'a yazilir.
    """

    TRACKED_APPS = {
        "chrome.exe":    "browser",
        "firefox.exe":   "browser",
        "msedge.exe":    "browser",
        "brave.exe":     "browser",
        "opera.exe":     "browser",
        "code.exe":      "coding",
        "cursor.exe":    "coding",
        "pycharm64.exe": "coding",
        "devenv.exe":    "coding",
        "discord.exe":   "social",
        "slack.exe":     "social",
        "telegram.exe":  "social",
        "whatsapp.exe":  "social",
        "explorer.exe":  "system",
        "obs64.exe":     "media",
        "vlc.exe":       "media",
        "spotify.exe":   "media",
    }

    BROWSERS = frozenset({"chrome.exe", "firefox.exe", "msedge.exe", "brave.exe", "opera.exe"})
    DISTRACT_LIMIT_KEYS = ("youtube", "reels", "tiktok")

    def __init__(self):
        self._usage: dict[str, float] = {}
        self._categories: dict[str, float] = {}
        self._last_check = datetime.now()
        self._last_active: dict = {}
        self._today = date.today().isoformat()
        self._load_daily()

    def is_available(self) -> bool:
        try:
            import win32gui  # noqa
            return True
        except ImportError:
            return False

    def _load_daily(self):
        self._reset_if_new_day()
        if not os.path.exists(SCREEN_DAILY_PATH):
            return
        try:
            with open(SCREEN_DAILY_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") != self._today:
                return
            self._usage = {k: float(v) for k, v in (data.get("by_app") or {}).items()}
            self._categories = {k: float(v) for k, v in (data.get("by_category") or {}).items()}
            self._last_active = data.get("last_active") or {}
        except Exception as e:
            logger.warning(f"[ScreenMonitor] screen_daily.json okunamadi: {e}")

    def _persist(self):
        try:
            os.makedirs(os.path.dirname(SCREEN_DAILY_PATH), exist_ok=True)
            with open(SCREEN_DAILY_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "date":         self._today,
                    "by_app":       {k: round(v, 1) for k, v in self._usage.items()},
                    "by_category":  {k: round(v, 1) for k, v in self._categories.items()},
                    "last_active":  self._last_active,
                    "updated_at":   datetime.now().isoformat(),
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[ScreenMonitor] kayit hatasi: {e}")

    def reset_daily_if_needed(self):
        """Gece yarisinda cagrilir."""
        if date.today().isoformat() != self._today:
            self._usage = {}
            self._categories = {}
            self._last_active = {}
            self._today = date.today().isoformat()
            self._persist()
            logger.info("[ScreenMonitor] Yeni gun — sayaclar sifirlandi.")

    def _reset_if_new_day(self):
        self._today = date.today().isoformat()

    def get_active_window(self) -> dict:
        if not self.is_available():
            return {"available": False}
        try:
            import win32gui, win32process
            import psutil

            hwnd    = win32gui.GetForegroundWindow()
            _, pid  = win32process.GetWindowThreadProcessId(hwnd)
            process = psutil.Process(pid)
            proc    = process.name().lower()
            title   = win32gui.GetWindowText(hwnd)

            return {
                "available": True,
                "title":     title,
                "process":   proc,
                "pid":       pid,
                "category":  self.TRACKED_APPS.get(proc, "other"),
            }
        except Exception as e:
            logger.warning(f"[ScreenMonitor] Aktif pencere okunamadi: {e}")
            return {"available": False}

    @classmethod
    def distraction_from_window(cls, window: dict) -> Optional[str]:
        """Aktif pencereden youtube / reels / tiktok kategorisi."""
        if not window.get("available"):
            return None
        proc = window.get("process", "")
        if proc not in cls.BROWSERS:
            return None
        title = (window.get("title") or "").lower()
        if "tiktok" in title:
            return "tiktok"
        if "youtube" in title or "youtu.be" in title:
            return "youtube"
        if any(k in title for k in ("reels", "instagram", "reel")):
            return "reels"
        return None

    _SKIP_CLASSES = frozenset({
        "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
        "DV2ControlHost", "MsgrIMEWindowClass", "IME", "Button",
    })

    @classmethod
    def _clean_chrome_title(cls, title: str) -> str:
        t = (title or "").strip()
        for suffix in (" - Google Chrome", " — Google Chrome", " - Chromium"):
            if t.endswith(suffix):
                t = t[: -len(suffix)]
        return t.strip()

    def _list_monitors(self) -> list[dict]:
        import win32api

        raw: list[dict] = []
        for hmonitor, _hdc, rect in win32api.EnumDisplayMonitors():
            info = win32api.GetMonitorInfo(hmonitor)
            raw.append({
                "handle":  hmonitor,
                "device":  info.get("Device", ""),
                "primary": bool(info.get("Flags", 0) & 1),
                "rect":    info.get("Monitor") or rect,
            })
        raw.sort(key=lambda m: (not m["primary"], (m["rect"] or [0])[0]))
        for i, m in enumerate(raw, start=1):
            m["index"] = i
            m["label"] = f"Monitor {i}" + (" (birincil)" if m["primary"] else "")
        return raw

    def _enumerate_desktop_windows(self) -> list[dict]:
        import win32gui, win32process, win32api
        import psutil

        monitors = self._list_monitors()
        dev_to_idx = {m["device"]: m["index"] for m in monitors}
        foreground = win32gui.GetForegroundWindow()
        rows: list[dict] = []

        def _cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.IsIconic(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd).strip()
            if not title or len(title) < 2:
                return True
            cls = win32gui.GetClassName(hwnd) or ""
            if cls in self._SKIP_CLASSES:
                return True
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                w, h = right - left, bottom - top
                if w < 180 or h < 120:
                    return True
            except Exception:
                return True
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                proc = psutil.Process(pid).name().lower()
            except Exception:
                return True
            try:
                mon = win32api.MonitorFromWindow(hwnd, 2)
                mi = win32api.GetMonitorInfo(mon)
                dev = mi.get("Device", "")
                mon_idx = dev_to_idx.get(dev, 1)
            except Exception:
                mon_idx = 1
            area = max(0, w) * max(0, h)
            rows.append({
                "hwnd":        hwnd,
                "title":       title[:120],
                "process":     proc,
                "monitor":     mon_idx,
                "area":        area,
                "foreground":  hwnd == foreground,
                "category":    self.TRACKED_APPS.get(proc, "other"),
            })
            return True

        win32gui.EnumWindows(_cb, None)
        rows.sort(key=lambda r: (-r["area"], r["title"].lower()))
        return rows

    def _chrome_tabs_cdp(self) -> Optional[list[dict]]:
        import requests

        ports = []
        env_port = (os.getenv("CHROME_DEBUG_PORT") or "").strip()
        if env_port:
            ports.append(int(env_port))
        for p in (9222, 9229):
            if p not in ports:
                ports.append(p)

        for port in ports:
            try:
                r = requests.get(f"http://127.0.0.1:{port}/json", timeout=1.2)
                if not r.ok:
                    continue
                tabs = []
                for item in r.json():
                    if item.get("type") != "page":
                        continue
                    title = (item.get("title") or "").strip()
                    url = (item.get("url") or "").strip()
                    if not title or title in ("New Tab", "Yeni Sekme"):
                        continue
                    tabs.append({
                        "title":  title[:100],
                        "url":    url[:120],
                        "active": False,
                        "source": "cdp",
                    })
                if tabs:
                    return tabs
            except Exception:
                continue
        return None

    def _chrome_tabs_uia(self, fg_hwnd: int | None = None) -> Optional[list[dict]]:
        try:
            import uiautomation as auto
        except ImportError:
            return None

        tabs: list[dict] = []
        try:
            root = auto.ControlFromHandle(fg_hwnd) if fg_hwnd else auto.GetForegroundControl()
            if not root:
                return None

            def _walk(ctrl, depth: int = 0):
                if depth > 14 or len(tabs) > 40:
                    return
                try:
                    if ctrl.ControlType == auto.ControlType.TabItemControl:
                        name = (ctrl.Name or "").strip()
                        if name and name not in ("New Tab", "Yeni Sekme", "+"):
                            active = False
                            try:
                                pat = ctrl.GetSelectionItemPattern()
                                active = bool(pat and pat.IsSelected)
                            except Exception:
                                pass
                            tabs.append({"title": name[:100], "active": active, "source": "uia"})
                    for ch in ctrl.GetChildren():
                        _walk(ch, depth + 1)
                except Exception:
                    return

            _walk(root)
            if tabs:
                seen, uniq = set(), []
                for t in tabs:
                    key = t["title"].lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    uniq.append(t)
                return uniq
        except Exception as e:
            logger.debug(f"[ScreenMonitor] Chrome UIA sekmeleri: {e}")
        return None

    def get_chrome_tabs(self, fg_hwnd: int | None = None, fg_title: str = "") -> list[dict]:
        """Chrome sekmeleri: CDP -> UIA -> acik Chrome pencereleri."""
        tabs = self._chrome_tabs_cdp()
        if not tabs:
            tabs = self._chrome_tabs_uia(fg_hwnd) or []

        if tabs and fg_title:
            fg_low = fg_title.lower()
            for t in tabs:
                if not t.get("active"):
                    tt = (t.get("title") or "").lower()
                    if tt and (tt in fg_low or fg_low.startswith(tt)):
                        t["active"] = True

        if not tabs:
            for win in self._enumerate_desktop_windows():
                if win.get("process") != "chrome.exe":
                    continue
                title = self._clean_chrome_title(win.get("title", ""))
                if not title or title in ("Google Chrome", "New Tab", "Yeni Sekme"):
                    continue
                tabs.append({
                    "title":  title[:100],
                    "active": bool(win.get("foreground")),
                    "source": "window",
                })

        if tabs and not any(t.get("active") for t in tabs):
            tabs[0]["active"] = True
        return tabs[:30]

    def get_live_desktop_snapshot(self) -> dict:
        """Tum monitörlerdeki gorunur pencereler + Chrome sekmeleri."""
        if not self.is_available():
            return {"available": False}

        foreground = self.get_active_window()
        monitors = self._list_monitors()
        windows = self._enumerate_desktop_windows()

        by_monitor: dict[int, list[dict]] = {m["index"]: [] for m in monitors}
        seen_titles: dict[int, set] = {m["index"]: set() for m in monitors}

        for win in windows:
            idx = win.get("monitor", 1)
            key = (win.get("process", ""), win.get("title", "").lower())
            if key in seen_titles.setdefault(idx, set()):
                continue
            seen_titles[idx].add(key)
            by_monitor.setdefault(idx, []).append(win)

        monitor_blocks = []
        for m in monitors:
            idx = m["index"]
            items = by_monitor.get(idx, [])[:10]
            monitor_blocks.append({
                "label":   m["label"],
                "index":   idx,
                "primary": m["primary"],
                "windows": items,
            })

        chrome_tabs = []
        fg_proc = (foreground.get("process") or "") if foreground.get("available") else ""
        has_chrome = fg_proc == "chrome.exe" or any(
            w.get("process") == "chrome.exe" for w in windows
        )
        if has_chrome:
            fg_hwnd = next((w["hwnd"] for w in windows if w.get("foreground")), None)
            fg_title = foreground.get("title", "") if foreground.get("available") else ""
            chrome_tabs = self.get_chrome_tabs(fg_hwnd=fg_hwnd, fg_title=fg_title)

        return {
            "available":  True,
            "foreground": foreground if foreground.get("available") else {},
            "monitors":   monitor_blocks,
            "chrome_tabs": chrome_tabs,
        }

    @classmethod
    def format_live_snapshot_lines(cls, snapshot: dict) -> list[str]:
        if not snapshot.get("available"):
            return []

        lines: list[str] = []
        distract_labels = {"youtube": "YouTube", "reels": "Reels/IG", "tiktok": "TikTok"}

        fg = snapshot.get("foreground") or {}
        if fg.get("title") or fg.get("process"):
            title = (fg.get("title") or "").strip()[:80]
            proc = fg.get("process", "")
            distract = cls.distraction_from_window(fg)
            tag = f" [{distract_labels.get(distract, distract)}]" if distract else ""
            lines.append(f"- Odak (yazdigin pencere): {title} ({proc}){tag}")

        for block in snapshot.get("monitors") or []:
            label = block.get("label", "Monitor")
            wins = block.get("windows") or []
            if not wins:
                lines.append(f"- {label}: (gorunur pencere yok)")
                continue
            lines.append(f"- {label}:")
            for w in wins:
                mark = " *" if w.get("foreground") else ""
                title = (w.get("title") or "")[:70]
                proc = w.get("process", "")
                lines.append(f"  - {title} ({proc}){mark}")

        tabs = snapshot.get("chrome_tabs") or []
        if tabs:
            lines.append("- Chrome sekmeleri:")
            for t in tabs:
                name = (t.get("title") or "")[:70]
                mark = " [aktif]" if t.get("active") else ""
                url = (t.get("url") or "").strip()
                if url and url.startswith("http"):
                    lines.append(f"  - {name}{mark} — {url[:80]}")
                else:
                    lines.append(f"  - {name}{mark}")

        return lines

    def tick(self):
        now     = datetime.now()
        elapsed = (now - self._last_check).total_seconds()
        self._last_check = now

        if date.today().isoformat() != self._today:
            self.reset_daily_if_needed()

        window = self.get_active_window()
        if not window.get("available"):
            return

        proc = window.get("process", "")
        if proc:
            self._usage[proc] = self._usage.get(proc, 0.0) + elapsed

        distract = self.distraction_from_window(window)
        if distract:
            self._categories[distract] = self._categories.get(distract, 0.0) + elapsed

        self._last_active = {
            "title":    window.get("title", "")[:120],
            "process":  proc,
            "category": window.get("category", "other"),
        }
        self._persist()

    def get_usage_minutes(self) -> dict[str, float]:
        return {k: round(v / 60, 1) for k, v in self._usage.items()}

    def get_category_minutes(self) -> dict[str, float]:
        return {k: round(v / 60, 1) for k, v in self._categories.items()}

    def get_daily_summary(self) -> dict:
        cats = self.get_category_minutes()
        return {
            "date":       self._today,
            "categories": cats,
            "by_app":     self.get_usage_minutes(),
            "active":     self._last_active,
            "total_distraction_min": round(sum(cats.get(k, 0) for k in self.DISTRACT_LIMIT_KEYS), 1),
        }

    def is_distraction_active(self) -> bool:
        return self.distraction_from_window(self.get_active_window()) is not None

    def check_limit_exceeded(self, habits: dict) -> Optional[str]:
        """Asilan platform adini dondurur: youtube | reels | tiktok"""
        limits = habits.get("screen_limits", {})
        cats   = self.get_category_minutes()
        for key in self.DISTRACT_LIMIT_KEYS:
            used = cats.get(key, 0)
            cap  = limits.get(f"{key}_minutes", 60 if key == "youtube" else 30)
            if used >= cap:
                return key
        return None

    @staticmethod
    def load_saved_summary() -> dict:
        """Kairos disinda (context) okumak icin."""
        if not os.path.exists(SCREEN_DAILY_PATH):
            return {}
        try:
            with open(SCREEN_DAILY_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") != date.today().isoformat():
                return {}
            cats = {
                k: round(float(v) / 60, 1)
                for k, v in (data.get("by_category") or {}).items()
            }
            apps = {
                k: round(float(v) / 60, 1)
                for k, v in (data.get("by_app") or {}).items()
            }
            return {
                "date":       data.get("date"),
                "categories": cats,
                "by_app":     apps,
                "active":     data.get("last_active") or {},
            }
        except Exception:
            return {}

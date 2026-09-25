#!/usr/bin/env python3
"""
EPIS -- Nightly Recalculation Engine
=====================================
Her gece calisir. Gunun ham verisini toplar, anonimlestirir,
Hybrid Pipeline uzerinden analiz eder, hafiza dosyalarini gunceller,
sabah raporu olusturur. Drift check en son asama olarak calisir.

Manuel calistirma : python nightly_recalculation.py
Kairos tarafindan : NightlyRecalculation().run()
"""

import os
import sys
import json
import sqlite3
import logging
from datetime import datetime, date, timedelta
from typing import Optional

THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
EPIS_ROOT = os.path.normpath(os.path.join(THIS_DIR, "..", ".."))

MEMORY_DIR   = os.path.join(EPIS_ROOT, "Layer-1", "memory")
IDENTITY_DIR = os.path.join(EPIS_ROOT, "Layer-1", "identity")
HABITS_DIR   = os.path.join(EPIS_ROOT, "Layer-1", "habits")
DB_PATH      = os.path.join(MEMORY_DIR, "lifetime.db")

sys.path.insert(0, THIS_DIR)
from router import EpisRouter
from crypto_layer import get_cipher, load_json_file
from memory_vault import LocalMemoryVault
from nc_transcript_client import CloudTranscriptClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - NIGHTLY - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(EPIS_ROOT, "nightly.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("EPIS.NIGHTLY")


# ===============================================================
# NIGHTLY CONVERSATION
# ===============================================================

class NightlyConversation:

    MAX_TURNS = 3

    def __init__(self, model: str, router: EpisRouter, context: dict = None):
        self.model   = model
        self.router  = router
        self.context = context or {}
        self.history = []
        self.turn    = 0

    def send(self, message: str) -> str:
        if self.turn >= self.MAX_TURNS:
            logger.warning(f"[{self.model}] Max tur asildi -- son yanit kullaniliyor.")
            return self.last_response()

        full_prompt = self._build_prompt(message)
        response    = self.router._dispatch_to_layer3(
            model=self.model, payload=full_prompt, context=self.context,
        )

        self.history.append({"role": "epis",  "content": message})
        self.history.append({"role": "model", "content": response})
        self.turn += 1

        logger.info(f"[{self.model}] Tur {self.turn}/{self.MAX_TURNS} -- {len(response)} kar.")
        return response

    def last_response(self) -> str:
        for entry in reversed(self.history):
            if entry["role"] == "model":
                return entry["content"]
        return ""

    def conversation_log(self) -> list:
        return self.history

    def _build_prompt(self, new_message: str) -> str:
        if not self.history:
            return new_message
        recent       = self.history[-4:]
        history_text = "\n\n".join([
            f"{'EPIS' if h['role'] == 'epis' else 'MODEL'}: "
            f"{h['content'][:600]}{'...' if len(h['content']) > 600 else ''}"
            for h in recent
        ])
        return (
            f"KONUSMA GECMISI (son {len(recent) // 2} tur):\n"
            f"{history_text}\n\n{'=' * 50}\n\n"
            f"EPIS (devam mesaji):\n{new_message}"
        )


# ===============================================================
# NIGHTLY RECALCULATION
# ===============================================================

class NightlyRecalculation:

    def __init__(
        self,
        *,
        day_id: str | None = None,
        transcript_client=None,
        vault=None,
    ):
        self.router         = EpisRouter()
        self.cipher         = get_cipher()
        self.requested_day_id = day_id or (os.getenv("EPIS_NC_DAY_ID") or "").strip() or None
        self.today          = date.fromisoformat(self.requested_day_id) if self.requested_day_id else date.today()
        self.run_id         = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.now_iso        = datetime.now().isoformat()
        self._pseudonym_map = {}
        self.transcript_client = transcript_client
        self.vault = vault or LocalMemoryVault(cipher=self.cipher)
        self.frozen_transcript: dict = {}
        self._safe_memory_evidence = ""

        self.report = {
            "date":              self.today.isoformat(),
            "run_id":            self.run_id,
            "status":            "running",
            "stages":            {},
            "conversation_logs": {},
            "highlights":        [],
            "errors":            [],
            "generated_at":      self.now_iso,
        }

    # ------------------------------------------------------------------
    # CANONICAL DAILY TRANSCRIPT / MEMORY VAULT BOUNDARY
    # ------------------------------------------------------------------

    def _get_transcript_client(self):
        if self.transcript_client is None:
            self.transcript_client = CloudTranscriptClient()
        return self.transcript_client

    def _freeze_canonical_day(self) -> dict | None:
        client = self._get_transcript_client()
        if self.requested_day_id:
            frozen = client.freeze(day_id=self.requested_day_id)
        else:
            # Nightly normally processes a completed day.  Asking for an older
            # pending day also makes offline-PC retries automatic.
            frozen = client.freeze(before_day_id=date.today().isoformat())
        if not frozen:
            return None
        day_id = str(frozen.get("day_id") or "")
        input_hash = str(frozen.get("input_hash") or "")
        if not day_id or len(input_hash) != 64:
            raise RuntimeError("invalid_frozen_transcript")
        self.today = date.fromisoformat(day_id)
        self.report["date"] = day_id
        self.report["transcript"] = {
            "day_id": day_id,
            "input_hash": input_hash,
            "message_count": len(frozen.get("messages") or []),
            "status": frozen.get("status"),
        }
        self.frozen_transcript = frozen
        return frozen

    def _finish_cloud_transcript(self, day_id: str, input_hash: str) -> dict:
        result = self._get_transcript_client().acknowledge_and_purge(
            day_id=day_id,
            input_hash=input_hash,
        )
        purged = int(result.get("purged") or 0)
        self.vault.mark_cloud_acked(day_id, input_hash, purged)
        self.report["stages"]["cloud_transcript_ack"] = f"OK (purged={purged})"
        return result

    # ------------------------------------------------------------------
    # ANA GIRIS NOKTASI
    # ------------------------------------------------------------------

    def run(self, sensor_data: dict = None) -> dict:
        logger.info(f"[{self.run_id}] Nightly Recalculation basliyor...")
        frozen = None
        day_id = None
        input_hash = None

        try:
            frozen = self._freeze_canonical_day()
            if not frozen:
                self.report["status"] = "partial"
                self.report["stages"]["daily_transcript"] = "NO_PENDING_DAY"
                self.report["highlights"].append("Islenecek tamamlanmis gunluk transcript yok.")
                return self._finalize(persist=False)

            day_id = str(frozen["day_id"])
            input_hash = str(frozen["input_hash"])
            self.report["stages"]["daily_transcript"] = "FROZEN"

            # Crash-safe retry: local DB commit already succeeded, but the cloud
            # ACK/purge may have failed last time. Do not call any model again.
            if self.vault.is_run_committed(day_id, input_hash):
                self.report["stages"]["memory_vault"] = "ALREADY_COMMITTED"
                try:
                    self._finish_cloud_transcript(day_id, input_hash)
                    self.report["status"] = "success"
                    self.report["highlights"].append("Onceki yerel NC commit'i cloud transcript ile uzlastirildi.")
                except Exception as ack_exc:
                    self.report["status"] = "partial"
                    self.report["stages"]["cloud_transcript_ack"] = "PENDING_RETRY"
                    self.report["errors"].append(str(ack_exc))
                return self._finalize()

            self.vault.note_run_started(day_id, input_hash, self.run_id)

            raw_data = self._collect_daily_data(sensor_data=sensor_data)
            self.report["stages"]["data_collection"] = "OK"

            if not raw_data.get("has_data"):
                logger.warning("Yeterli gunluk veri yok.")
                self.report["status"] = "partial"
                self.report["highlights"].append("Bugun icin yeterli veri toplanamadi.")
                self.vault.note_run_failed(day_id, input_hash, "no_daily_data")
                return self._finalize()

            safe_data = self._apply_privacy(raw_data)
            self.report["stages"]["privacy_layer"] = "OK"
            evidence_lines = []
            for item in safe_data.get("interactions", [])[:120]:
                if item.get("event_type") != "daily_transcript":
                    continue
                role = str(item.get("role") or "?")
                message_id = str(item.get("message_id") or "")
                body = str(item.get("raw_text") or "").strip().replace("\n", " ")
                if not body:
                    continue
                evidence_lines.append(f"[{message_id}] {role}: {body[:800]}")
            self._safe_memory_evidence = "\n".join(evidence_lines)[:18000]

            summary, s1_log = self._stage1_summarize(safe_data)
            self.report["stages"]["stage1_gemini"]     = f"OK ({s1_log['turns']} tur)"
            self.report["conversation_logs"]["stage1"] = s1_log

            if summary.startswith("HATA"):
                raise RuntimeError(f"Stage 1 basarisiz: {summary}")

            analysis_raw, s2_log = self._stage2_analyze(summary)
            self.report["stages"]["stage2_claude"]     = f"OK ({s2_log['turns']} tur)"
            self.report["conversation_logs"]["stage2"] = s2_log

            if analysis_raw.startswith("HATA"):
                raise RuntimeError(f"Stage 2 basarisiz: {analysis_raw}")

            analysis = self._parse_analysis(analysis_raw)

            summary  = self.router.privacy.deanonymize(summary, self._pseudonym_map)
            analysis = self._deanonymize_obj(analysis)

            epis_voice, s3_log = self._stage3_epis_voice(summary, analysis)
            self.report["stages"]["stage3_epis"] = (
                f"OK ({s3_log.get('chars', 0)} kar.)" if epis_voice else "SKIP"
            )
            self.report["conversation_logs"]["stage3"] = s3_log
            if epis_voice:
                self.report["epis_voice"] = epis_voice

            # AUTHORITATIVE LONG-TERM COMMIT.  Nothing on the cloud may be
            # marked processed/purged before this transaction succeeds.
            vault_result = self.vault.apply_nightly_result(
                day_id=day_id,
                input_hash=input_hash,
                run_id=self.run_id,
                summary=summary,
                analysis=analysis,
                epis_voice=epis_voice or "",
                transcript_messages=frozen.get("messages") or [],
            )
            self.report["stages"]["memory_vault"] = "OK"
            self.report["memory_vault"] = vault_result

            # Compatibility mirrors stay best-effort; identity_self.json is
            # intentionally never written by Nightly Recalculation.
            try:
                self._update_memory(analysis, summary, epis_voice=epis_voice or "")
                self.report["stages"]["legacy_memory_mirror"] = "OK"
            except Exception as legacy_exc:
                logger.warning(f"Legacy memory mirror: {legacy_exc}")
                self.report["stages"]["legacy_memory_mirror"] = "WARN"

            try:
                self._update_habits(analysis)
                self.report["stages"]["habit_update"] = "OK"
            except Exception as habit_exc:
                logger.warning(f"Legacy habit mirror: {habit_exc}")
                self.report["stages"]["habit_update"] = "WARN"

            try:
                drift_result = self._run_drift_check()
                self.report["stages"]["drift_check"] = drift_result
            except Exception as drift_exc:
                logger.warning(f"Drift check: {drift_exc}")
                self.report["stages"]["drift_check"] = "WARN"

            try:
                self._prepare_vault_sync()
                self.report["stages"]["vault_sync"] = "PENDING"
            except Exception as sync_exc:
                logger.warning(f"Legacy vault sync prep: {sync_exc}")
                self.report["stages"]["vault_sync"] = "WARN"

            self.report["status"]     = "success"
            self.report["highlights"] = analysis.get("behavioral_insights", [])[:3]
            if analysis.get("tomorrow_context"):
                self.report["tomorrow_context"] = analysis["tomorrow_context"]

            if epis_voice:
                try:
                    pending_path = os.path.join(MEMORY_DIR, "pending.json")
                    self._append_pending(
                        pending_path=pending_path,
                        trigger_type="morning_brief",
                        message=epis_voice,
                        priority="normal",
                    )
                except Exception as pe:
                    logger.warning(f"morning_brief pending yazilamadi: {pe}")

            # Cloud ACK is deliberately last. If it fails, local memory remains
            # committed and the frozen raw transcript stays on Postgres for retry.
            try:
                self._finish_cloud_transcript(day_id, input_hash)
            except Exception as ack_exc:
                logger.warning(f"Cloud transcript ACK beklemede: {ack_exc}")
                self.report["status"] = "partial"
                self.report["stages"]["cloud_transcript_ack"] = "PENDING_RETRY"
                self.report["errors"].append(str(ack_exc))

        except Exception as e:
            logger.error(f"Kritik hata: {e}", exc_info=True)
            self.report["status"] = "failed"
            self.report["errors"].append(str(e))
            if day_id and input_hash and not self.vault.is_run_committed(day_id, input_hash):
                try:
                    self.vault.note_run_failed(day_id, input_hash, str(e))
                except Exception:
                    pass
            self._handle_failure(str(e))

        return self._finalize()

    def _new_backlog_worker(self, day_id: str, client):
        return NightlyRecalculation(
            day_id=day_id,
            transcript_client=client,
            vault=self.vault,
        )

    def run_backlog(
        self,
        sensor_data: dict = None,
        *,
        max_days: int | None = None,
    ) -> dict:
        """Drain completed pending days oldest-first on the trusted local node.

        Kairos may be offline for multiple nights.  A single wake/start should
        therefore catch up a bounded backlog instead of processing one day per
        future night forever.  Each day still uses the exact same freeze ->
        local commit -> cloud ACK boundary implemented by ``run``.
        """
        if self.requested_day_id:
            return self.run(sensor_data=sensor_data)

        client = self._get_transcript_client()
        before_day_id = date.today().isoformat()
        pending = client.pending_days(before_day_id=before_day_id)
        if not pending:
            return {
                "date": before_day_id,
                "run_id": self.run_id,
                "status": "idle",
                "stages": {"daily_transcript": "NO_PENDING_DAY"},
                "processed_days": [],
                "errors": [],
                "generated_at": datetime.now().isoformat(),
            }

        if max_days is None:
            try:
                max_days = int(os.getenv("EPIS_NC_MAX_DAYS_PER_RUN", "7"))
            except ValueError:
                max_days = 7
        max_days = max(1, min(int(max_days), 31))

        reports: list[dict] = []
        for item in pending[:max_days]:
            target_day = str(item.get("day_id") or "").strip()
            if not target_day:
                continue
            worker = self._new_backlog_worker(target_day, client)
            # ``sensor_data`` represents the currently running machine/day.
            # Backlog days are already completed, so attaching today's live
            # sensor snapshot to an older date would create false evidence.
            report = worker.run(sensor_data=None)
            reports.append(report)
            if report.get("status") != "success":
                break

        processed_days = [
            str(report.get("date") or "")
            for report in reports
            if report.get("status") == "success"
        ]
        failed = next((r for r in reports if r.get("status") == "failed"), None)
        partial = next((r for r in reports if r.get("status") == "partial"), None)
        status = "failed" if failed else ("partial" if partial else "success")
        errors = []
        for report in reports:
            errors.extend(report.get("errors") or [])
        return {
            "date": reports[-1].get("date") if reports else before_day_id,
            "run_id": self.run_id,
            "status": status,
            "stages": {
                "backlog": f"{len(processed_days)}/{min(len(pending), max_days)} processed",
                "remaining_hint": max(0, len(pending) - len(processed_days)),
            },
            "processed_days": processed_days,
            "reports": reports,
            "errors": errors,
            "generated_at": datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------
    # 1. VERI TOPLAMA
    # ------------------------------------------------------------------

    def _collect_daily_data(self, sensor_data: dict = None) -> dict:
        data = {
            "date":           self.today.isoformat(),
            "has_data":       False,
            "interactions":   [],
            "current_state":  {},
            "people":         {},
            "weekly_context": {},
            "biometrics":     sensor_data.get("biometrics", {}) if sensor_data else {},
            "screen_usage":   sensor_data.get("screen_usage", {}) if sensor_data else {},
            "source_status":  {},
        }
        if sensor_data:
            data["source_status"]["sensors"] = "Kairos tarafindan saglandi"

        # Canonical raw conversation comes from the frozen cloud transcript.
        transcript_messages = list(self.frozen_transcript.get("messages") or [])
        data["interactions"] = [
            {
                "timestamp": str(item.get("created_at") or ""),
                "event_type": "daily_transcript",
                "message_id": str(item.get("message_id") or ""),
                "role": str(item.get("role") or ""),
                "raw_text": str(item.get("text") or ""),
                "observation": "",
                "tags": json.dumps(
                    {"source": item.get("source"), "attachments": item.get("attachments") or []},
                    ensure_ascii=False,
                ),
            }
            for item in transcript_messages
            if str(item.get("text") or "").strip()
        ]
        data["source_status"]["daily_transcript"] = f"{len(data['interactions'])} mesaj"

        # Non-chat local events can still enrich the day, but legacy raw chat/session
        # rows are not re-imported into the new memory pipeline.
        try:
            if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0:
                conn   = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT timestamp, event_type, raw_text, epis_observation, ai_analysis_tags "
                    "FROM lifetime_log WHERE timestamp LIKE ? AND event_type NOT IN ('chat','session') "
                    "ORDER BY timestamp",
                    (f"{self.today.isoformat()}%",)
                )
                rows = cursor.fetchall()
                conn.close()
                data["interactions"].extend([
                    {"timestamp": r[0], "event_type": r[1],
                     "raw_text": self.cipher.decrypt_str(r[2] or ""),
                     "observation": self.cipher.decrypt_str(r[3] or ""),
                     "tags": r[4] or "[]"}
                    for r in rows
                ])
                data["source_status"]["lifetime_aux"] = f"{len(rows)} kayit"
        except Exception as e:
            logger.warning(f"Lifetime auxiliary DB: {e}")
            data["source_status"]["lifetime_aux"] = f"HATA: {e}"

        # current_state.json duz metin; people.json sifreli olabilir
        try:
            state = self._read_json(os.path.join(MEMORY_DIR, "current_state.json"))
            if state:
                data["current_state"] = state
                data["source_status"]["current_state"] = "OK"
        except Exception as e:
            logger.warning(f"current_state.json: {e}")

        try:
            people = load_json_file(os.path.join(MEMORY_DIR, "people.json"))
            if people:
                data["people"] = people
                data["source_status"]["people"] = "OK"
        except Exception as e:
            logger.warning(f"people.json: {e}")

        try:
            weekly = self._read_json(os.path.join(MEMORY_DIR, "weekly.json"))
            if weekly:
                days = weekly.get("days", [])
                data["weekly_context"] = {
                    "week_start":      weekly.get("week_start"),
                    "recent_days":     days[-3:],
                    "weekly_patterns": weekly.get("weekly_patterns", {}),
                }
                data["source_status"]["weekly"] = "OK"
        except Exception as e:
            logger.warning(f"weekly.json: {e}")

        data["has_data"] = bool(
            data["interactions"]
            or (data["current_state"] and len(data["current_state"]) > 1)
            or data["biometrics"].get("available")
        )
        return data

    # ------------------------------------------------------------------
    # 2. PRIVACY LAYER
    # ------------------------------------------------------------------

    def _apply_privacy(self, raw_data: dict) -> dict:
        raw_str = json.dumps(raw_data, ensure_ascii=False)
        # Anonimleştir VE geri-çevirme haritasını sakla (analiz yerelde geri çevrilecek)
        safe_str, self._pseudonym_map = self.router.privacy.anonymize(raw_str)
        try:
            return json.loads(safe_str)
        except json.JSONDecodeError:
            return {"raw_text": safe_str, "date": raw_data["date"], "has_data": True}

    def _deanonymize_obj(self, obj):
        """Bir dict/str içindeki takma adları gerçek adlara çevirir (yerel saklama için)."""
        if not self._pseudonym_map:
            return obj
        try:
            as_str = json.dumps(obj, ensure_ascii=False)
            restored = self.router.privacy.deanonymize(as_str, self._pseudonym_map)
            return json.loads(restored)
        except Exception:
            return obj

    # ------------------------------------------------------------------
    # 3. STAGE 1 -- GEMINI
    # ------------------------------------------------------------------

    def _stage1_summarize(self, safe_data: dict) -> tuple[str, dict]:
        model = self.router.routing_table.get("hybrid_stage_1", "gemini-2.0-flash")
        conv  = NightlyConversation(
            model=model, router=self.router,
            context={"stage": "nightly_stage1", "date": self.today.isoformat()},
        )

        interaction_count = len(safe_data.get("interactions", []))
        biometrics        = safe_data.get("biometrics", {})
        screen_usage      = safe_data.get("screen_usage", {})

        initial = f"""Sen bir veri ozet motorusun. Kisisel yorum yapma.

TARIH: {safe_data.get('date')}
ETKILESIM SAYISI: {interaction_count}

ETKILESIM LOGLARI:
{json.dumps(safe_data.get('interactions', []), ensure_ascii=False, indent=2)}

ANLIKA DURUM:
{json.dumps(safe_data.get('current_state', {}), ensure_ascii=False, indent=2)}

BIYOMETRIK VERI:
{json.dumps(biometrics, ensure_ascii=False, indent=2)}

EKRAN KULLANIMI (dakika):
{json.dumps(screen_usage, ensure_ascii=False, indent=2)}

HAFTALIK BAGLAM:
{json.dumps(safe_data.get('weekly_context', {}), ensure_ascii=False, indent=2)}

Sunlari cikar:
1. Saatlik aktivite dagilimi
2. One cikan etkilesimler
3. Duygusal ton ipuclari
4. Enerji ve odak tahminleri
5. Tekrar eden ornuntler veya anomaliler
6. Bu haftayla karsilastirma

Kisisel isim KULLANMA -- [KNOWN_USER] formatini koru."""

        response   = conv.send(initial)
        evaluation = self._evaluate_stage1(response)

        if not evaluation["ok"] and evaluation["followup"]:
            logger.info(f"Stage 1 follow-up: {evaluation['issues']}")
            response   = conv.send(evaluation["followup"])
            evaluation = self._evaluate_stage1(response)

        if not evaluation["ok"] and evaluation["followup"]:
            logger.info("Stage 1 son tur.")
            conv.send(evaluation["followup"])

        log = {
            "turns": conv.turn, "model": model,
            "history": [{"role": h["role"], "length": len(h["content"])} for h in conv.conversation_log()],
        }
        return conv.last_response(), log

    def _evaluate_stage1(self, response: str) -> dict:
        if not response or response.startswith("HATA"):
            return {"ok": False, "issues": ["api_hatasi"], "followup": "Lutfen gunluk aktivite ozetini tekrar olustur."}

        issues  = []
        if len(response) < 250:
            issues.append("cok_kisa")
        keywords = ["aktivite", "etkilesim", "enerji", "odak"]
        missing  = [k for k in keywords if k not in response.lower()]
        if len(missing) >= 3:
            issues.append(f"eksik_bolumler: {missing}")

        if not issues:
            return {"ok": True, "issues": [], "followup": None}

        followup_parts = []
        if "cok_kisa" in issues:
            followup_parts.append("Ozet cok kisa kaldi. Her bolumu daha detayli isle.")
        if any("eksik" in i for i in issues):
            followup_parts.append(f"Su konular eksik: {missing}. Bunlari da ekle.")

        return {"ok": False, "issues": issues, "followup": " ".join(followup_parts)}

    # ------------------------------------------------------------------
    # 4. STAGE 2 -- CLAUDE OPUS
    # ------------------------------------------------------------------

    def _stage2_analyze(self, summary: str) -> tuple[str, dict]:
        model = self.router.routing_table.get("hybrid_stage_2", "claude-opus-4-8")
        conv  = NightlyConversation(
            model=model, router=self.router,
            context={"stage": "nightly_stage2", "date": self.today.isoformat()},
        )

        json_schema = """{
  "date": "<YYYY-MM-DD>",
  "mood_estimate": "pozitif | notral | negatif | karisik",
  "energy_level": "yuksek | orta | dusuk",
  "focus_quality": "derin | orta | daginik",
  "stress_signal": "yok | hafif | orta | yuksek",
  "key_events": ["olay 1", "olay 2"],
  "memory_candidates": [
    {
      "kind": "fact | preference | goal | project | commitment | relationship | other",
      "subject": "user veya ilgili konu/kisi",
      "content": "gelecekte gerçekten yararlı olacak açık bilgi",
      "confidence": 0.0,
      "evidence_message_ids": ["mesaj-id"]
    }
  ],
  "habits": {
    "confirmed": [], "new_detected": [], "changed": [], "broken": []
  },
  "behavioral_insights": ["gozlem 1", "gozlem 2", "gozlem 3"],
  "anomalies": [],
  "epis_learnings": ["kalici ogrenme 1", "kalici ogrenme 2"],
  "tomorrow_context": "yarin icin baglam",
  "weekly_contribution": "haftanin genel ornuntusune katkisi"
}"""

        initial = f"""Sen bir davranis analisti ve psikoloji uzmanissin.
[KNOWN_USER] adli kullanicinin bugunune ait ozet veri:

{summary}

HAM GUNLUK KANIT (privacy katmanindan gecmis, yalnizca hafiza adaylarini dogrulamak icin):
{self._safe_memory_evidence or "(yok)"}

KALICI HAFIZA KURALLARI:
- memory_candidates sadece gelecekte baska bir gunde ise yarayacak acik/kuvvetli bilgileri icersin.
- Gecici test metinlerini, selamlasmayi, modelin kendi cevabini veya tahmini kalici bilgi yapma.
- URL/ayar/proje bilgisi ancak kullaniciya ait kalici bir sistem/proje gercegiyse saklanabilir.
- Duygu/karakter cikarimini kullanicinin kendi sozu gibi kaydetme; bunlar behavioral_insights tarafinda kalsin.
- Her aday icin mumkunse kanittaki gercek message_id degerlerini yaz.
- identity_self / kullanicinin kendi tanimladigi kimlik degerlerini degistirmeye calisma.

YALNIZCA asagidaki JSON formatinda yanitla. Baska hicbir sey yazma.

{json_schema}"""

        response   = conv.send(initial)
        evaluation = self._evaluate_stage2(response)

        if not evaluation["ok"] and evaluation["followup"]:
            logger.info(f"Stage 2 follow-up: {evaluation['issues']}")
            response   = conv.send(evaluation["followup"])
            evaluation = self._evaluate_stage2(response)

        if not evaluation["ok"] and evaluation["followup"]:
            logger.info("Stage 2 son tur.")
            conv.send(evaluation["followup"])

        log = {
            "turns": conv.turn, "model": model,
            "history": [{"role": h["role"], "length": len(h["content"])} for h in conv.conversation_log()],
        }
        return conv.last_response(), log

    def _evaluate_stage2(self, response: str) -> dict:
        if not response or response.startswith("HATA"):
            return {"ok": False, "issues": ["api_hatasi"], "followup": "Analizi JSON formatinda tekrar yap."}

        clean = response.strip()
        if clean.startswith("```"):
            lines = clean.split("\n")
            clean = "\n".join(lines[1:-1]) if len(lines) > 2 else clean

        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError:
            return {"ok": False, "issues": ["json_parse_hatasi"],
                    "followup": "Yanit JSON formatinda degil. Sadece gecerli JSON dondur."}

        required = {
            "mood_estimate": "duygu durumu",
            "energy_level": "enerji seviyesi",
            "behavioral_insights": "davranissal gozlemler",
            "epis_learnings": "EPIS ogrenmeleri",
        }
        empty = [desc for field, desc in required.items() if not parsed.get(field)]
        if not empty:
            return {"ok": True, "issues": [], "followup": None}

        return {"ok": False, "issues": [f"bos_alan: {empty}"],
                "followup": f"Sunlar bos kaldi: {', '.join(empty)}. Ayni JSON ile doldurarak gonder."}

    # ------------------------------------------------------------------
    # 4b. STAGE 3 — EPIS SESI (Layer-1 karakter)
    # ------------------------------------------------------------------

    def _stage3_epis_voice(self, summary: str, analysis: dict) -> tuple[str, dict]:
        """
        Ucuncu tur: ozet+analiz uzerine EPIS kendi sesiyle kisa yorum.
        Yerel Qwen (QWEN_BASE_URL) — RunPod kredisini yakmaz.
        """
        log = {"turns": 0, "model": os.getenv("QWEN_MODEL", "qwen3.5:9b"), "chars": 0}
        analysis_slim = {
            "mood_estimate":       analysis.get("mood_estimate"),
            "energy_level":        analysis.get("energy_level"),
            "stress_signal":       analysis.get("stress_signal"),
            "behavioral_insights": (analysis.get("behavioral_insights") or [])[:4],
            "tomorrow_context":    analysis.get("tomorrow_context"),
            "epis_learnings":      (analysis.get("epis_learnings") or [])[:3],
        }
        user_msg = (
            "Gece analizi bitti. kullanıcıya sabah iletecegin kisa yorumu yaz.\n\n"
            f"## Stage-1 ozet\n{summary[:1400]}\n\n"
            f"## Stage-2 analiz\n{json.dumps(analysis_slim, ensure_ascii=False, indent=2)}\n\n"
            "Kurallar:\n"
            "- Sen EPIS'sin; kullanıcının uyku/beden/olcum verisini kendi yasantin gibi sahiplenme.\n"
            "- 2-5 cumle, samimi, kisa. Yargilama / vaaz yok.\n"
            "- Ne fark ettigini ve yarin icin tek bir not ekle.\n"
            '- Yanit YALNIZCA JSON: {"type":"direct","message":"..."}'
        )

        prev_think = os.environ.get("QWEN_THINK")
        os.environ["QWEN_THINK"] = "false"
        try:
            from epis_core import build_system_prompt, Layer1Engine

            engine = Layer1Engine(build_system_prompt(), backend="qwen")
            parsed = engine.send(user_msg)
            log["turns"] = 1
            msg = (parsed.get("message") or "").strip()
            if not msg and parsed.get("type") == "tool_call":
                msg = (parsed.get("bridge_message") or "").strip()
            log["chars"] = len(msg)
            if msg:
                logger.info(f"Stage 3 EPIS sesi: {len(msg)} kar.")
            else:
                logger.warning("Stage 3: bos EPIS sesi")
            return msg, log
        except Exception as e:
            logger.warning(f"Stage 3 atlandi (Layer-1/Qwen): {e}")
            log["error"] = str(e)[:200]
            return "", log
        finally:
            if prev_think is None:
                os.environ.pop("QWEN_THINK", None)
            else:
                os.environ["QWEN_THINK"] = prev_think

    # ------------------------------------------------------------------
    # 5. ANALIZ PARSE
    # ------------------------------------------------------------------

    def _parse_analysis(self, raw: str) -> dict:
        clean = raw.strip()
        if clean.startswith("```"):
            lines = clean.split("\n")
            clean = "\n".join(lines[1:-1]) if len(lines) > 2 else clean
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            logger.warning("Son analiz parse basarisiz -- fallback dict.")
            return {
                "date": self.today.isoformat(),
                "mood_estimate": "bilinmiyor", "energy_level": "bilinmiyor",
                "focus_quality": "bilinmiyor", "stress_signal": "bilinmiyor",
                "key_events": [], "memory_candidates": [],
                "habits": {"confirmed": [], "new_detected": [], "changed": [], "broken": []},
                "behavioral_insights": [raw[:400]],
                "anomalies": [], "epis_learnings": [],
                "tomorrow_context": "", "weekly_contribution": "",
                "parse_error": True,
            }

    # ------------------------------------------------------------------
    # 6. HAFIZA GUNCELLEME
    # ------------------------------------------------------------------

    def _update_memory(self, analysis: dict, summary: str, epis_voice: str = ""):
        weekly_path = os.path.join(MEMORY_DIR, "weekly.json")
        weekly      = self._read_json(weekly_path) or {
            "week_start": self._week_start(), "week_end": self._week_end(),
            "days": [], "weekly_patterns": {}, "last_updated": self.now_iso,
        }

        today_str      = self.today.isoformat()
        weekly["days"] = [d for d in weekly.get("days", []) if d.get("date") != today_str]
        day_row = {
            "date": today_str,
            "summary": summary[:800],
            "analysis": analysis,
        }
        if epis_voice:
            day_row["epis_voice"] = epis_voice[:1200]
        weekly["days"].append(day_row)
        weekly["last_updated"] = self.now_iso
        self._write_json(weekly_path, weekly)
        logger.info("weekly.json guncellendi.")

        if self.today.weekday() == 6:
            self._monthly_rollup(weekly)

        if analysis.get("epis_learnings"):
            self._update_epis_self(analysis["epis_learnings"])

        if epis_voice:
            self._update_epis_self(
                [f"[EPIS sesi] {epis_voice[:400]}"],
                source="nightly_epis_voice",
            )

        self._update_identity_calculated(analysis)

    def _monthly_rollup(self, weekly: dict):
        monthly_path = os.path.join(MEMORY_DIR, "monthly.json")
        monthly      = self._read_json(monthly_path) or {"months": {}, "last_updated": self.now_iso}
        month_key    = self.today.strftime("%Y-%m")
        monthly.setdefault("months", {}).setdefault(month_key, {"weeks": []})["weeks"].append({
            "week_start": weekly.get("week_start"), "week_end": weekly.get("week_end"),
            "day_count": len(weekly.get("days", [])), "added_at": self.now_iso,
        })
        monthly["last_updated"] = self.now_iso
        self._write_json(monthly_path, monthly)
        logger.info("monthly.json rollup tamamlandi.")

    def _update_epis_self(self, learnings: list, source: str = "nightly_recalculation"):
        self_path = os.path.join(IDENTITY_DIR, "epis_self.json")
        self_data = self._read_json(self_path) or {"conclusions": [], "last_updated": self.now_iso}
        for item in learnings:
            if item and isinstance(item, str):
                self_data["conclusions"].append({
                    "date": self.today.isoformat(), "conclusion": item,
                    "source": source, "run_id": self.run_id,
                })
        self_data["last_updated"] = self.now_iso
        self._write_json(self_path, self_data)
        logger.info(f"epis_self.json: {len(learnings)} ogrenme eklendi ({source}).")

    def _update_identity_calculated(self, analysis: dict):
        calc_path = os.path.join(IDENTITY_DIR, "identity_calculated.json")
        calc_data = self._read_json(calc_path) or {"trait_history": [], "last_calculated": self.now_iso}
        calc_data["trait_history"].append({
            "date":                self.today.isoformat(),
            "mood_estimate":       analysis.get("mood_estimate"),
            "energy_level":        analysis.get("energy_level"),
            "focus_quality":       analysis.get("focus_quality"),
            "stress_signal":       analysis.get("stress_signal"),
            "behavioral_insights": analysis.get("behavioral_insights", []),
            "anomalies":           analysis.get("anomalies", []),
        })
        calc_data["trait_history"]   = calc_data["trait_history"][-30:]
        calc_data["last_calculated"] = self.now_iso
        self._write_json(calc_path, calc_data)
        logger.info("identity_calculated.json guncellendi.")

    # ------------------------------------------------------------------
    # 7. ALISKANLIK GUNCELLEME
    # ------------------------------------------------------------------

    def _update_habits(self, analysis: dict):
        habits_data = analysis.get("habits", {})
        if not any(habits_data.values()):
            return
        os.makedirs(HABITS_DIR, exist_ok=True)
        habits_path = os.path.join(HABITS_DIR, "habit_log.json")
        log         = self._read_json(habits_path) or {"entries": [], "last_updated": self.now_iso}
        log["entries"].append({
            "date": self.today.isoformat(), "run_id": self.run_id,
            "confirmed":    habits_data.get("confirmed", []),
            "new_detected": habits_data.get("new_detected", []),
            "changed":      habits_data.get("changed", []),
            "broken":       habits_data.get("broken", []),
        })
        log["entries"]      = log["entries"][-90:]
        log["last_updated"] = self.now_iso
        self._write_json(habits_path, log)
        logger.info("habit_log.json guncellendi.")

    # ------------------------------------------------------------------
    # 7b. DRIFT KONTROLU
    # ------------------------------------------------------------------

    def _run_drift_check(self) -> str:
        personality_path = os.path.join(IDENTITY_DIR, "epis_personality.md")
        self_path        = os.path.join(IDENTITY_DIR, "epis_self.json")

        if not os.path.exists(personality_path) or not os.path.exists(self_path):
            return "ATLANDI (dosya yok)"

        try:
            self_data = self._read_json(self_path)
            recent    = self_data.get("conclusions", [])[-10:]
            if not recent:
                return "ATLANDI (epis_self bos)"

            with open(personality_path, "r", encoding="utf-8") as f:
                personality_text = f.read()

            conclusions_text = "\n".join(f"- {c.get('conclusion', '')}" for c in recent)
            prompt = (
                f"EPIS karakter belgesi:\n{personality_text[:2000]}\n\n"
                f"Son hafta EPIS ogrenmeleri:\n{conclusions_text}\n\n"
                "Bu ogrenmeleri karakter belgesiyle karsilastir. "
                "Sapma varsa 2-3 cumleyle belirt. "
                "Sapma yoksa sadece 'Sapma tespit edilmedi.' yaz."
            )

            drift_model = self.router.routing_table.get(
                "hybrid_stage_2",
                self.router.routing_table.get("deep_analysis", "claude-opus-4-8"),
            )
            result = self.router._dispatch_to_layer3(
                model=drift_model,
                payload=self.router._apply_privacy_layer(prompt),
                context={"stage": "drift_check", "date": self.today.isoformat()},
            )

            if "sapma tespit edilmedi" not in result.lower():
                pending_path = os.path.join(MEMORY_DIR, "pending.json")
                self._append_pending(
                    pending_path=pending_path,
                    trigger_type="drift",
                    message=f"Haftalik karakter kontrolu: {result[:500]}",
                    priority="high",
                )
                logger.info("Drift tespit edildi -- pending'e yazildi.")
                return "DRIFT TESPIT EDILDI"

            logger.info("Drift yok.")
            return "OK (sapma yok)"

        except Exception as e:
            logger.error(f"Drift check hatasi: {e}")
            return f"HATA: {e}"

    def _append_pending(self, pending_path, trigger_type, message, priority):
        import uuid as _uuid
        try:
            if os.path.exists(pending_path):
                with open(pending_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = {"items": []}
            item = {
                "id":           str(_uuid.uuid4()),
                "created_at":   self.now_iso,
                "trigger_type": trigger_type,
                "context":      message,
                "message":      message,
                "priority":     priority,
                "status":       "pending",
            }
            data["items"].append(item)
            data["last_updated"] = self.now_iso
            os.makedirs(os.path.dirname(pending_path), exist_ok=True)
            with open(pending_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            try:
                habits = {}
                hp = os.path.join(HABITS_DIR, "habits.json")
                if os.path.exists(hp):
                    with open(hp, "r", encoding="utf-8") as hf:
                        habits = json.load(hf)
                from proactive_delivery import ProactiveDelivery
                push = ProactiveDelivery(habits=habits).apply_push_to_item(item)
                if push.get("pushed"):
                    with open(pending_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["items"][-1] = item
                    data["last_updated"] = self.now_iso
                    with open(pending_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    logger.info("Drift pending -- aninda WA push gonderildi.")
            except Exception as pe:
                logger.warning(f"Drift aninda push atlandi: {pe}")
        except Exception as e:
            logger.error(f"Pending yazma hatasi: {e}")

    # ------------------------------------------------------------------
    # 8. VAULT SYNC
    # ------------------------------------------------------------------

    def _prepare_vault_sync(self):
        candidates = [
            os.path.join(MEMORY_DIR,   "weekly.json"),
            os.path.join(MEMORY_DIR,   "monthly.json"),
            os.path.join(MEMORY_DIR,   "yearly.json"),
            os.path.join(MEMORY_DIR,   "lifetime.db"),
            os.path.join(IDENTITY_DIR, "epis_self.json"),
            os.path.join(IDENTITY_DIR, "identity_calculated.json"),
            os.path.join(HABITS_DIR,   "habit_log.json"),
        ]
        self._write_json(os.path.join(EPIS_ROOT, "pending_sync.json"), {
            "created_at": self.now_iso, "run_id": self.run_id,
            "status": "pending", "sync_after": self.today.isoformat(),
            "files": [f for f in candidates if os.path.exists(f)],
        })
        logger.info("pending_sync.json yazildi.")

    # ------------------------------------------------------------------
    # 9. GRACEFUL DEGRADATION
    # ------------------------------------------------------------------

    def _handle_failure(self, error_msg: str):
        logger.error("Graceful degradation -- onceki gun verisi korunuyor.")
        self.report["highlights"].append(
            f"NR tamamlanamadi. Onceki veri aktif. Hata: {error_msg[:200]}"
        )

    # ------------------------------------------------------------------
    # 10. FINALIZE
    # ------------------------------------------------------------------

    def _finalize(self, *, persist: bool = True) -> dict:
        self.report["generated_at"] = datetime.now().isoformat()
        if persist:
            self._write_json(os.path.join(MEMORY_DIR, "morning_report.json"), self.report)
        logger.info(
            f"[{self.run_id}] Bitti -- Durum: {self.report['status']} | "
            f"Asamalar: {self.report['stages']}"
        )
        return self.report

    # ------------------------------------------------------------------
    # YARDIMCILAR
    # ------------------------------------------------------------------

    def _read_json(self, path: str) -> Optional[dict]:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else None
        except Exception as e:
            logger.warning(f"JSON okuma ({os.path.basename(path)}): {e}")
            return None

    def _write_json(self, path: str, data: dict):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _week_start(self) -> str:
        return (self.today - timedelta(days=self.today.weekday())).isoformat()

    def _week_end(self) -> str:
        return (self.today + timedelta(days=6 - self.today.weekday())).isoformat()


# ===============================================================
if __name__ == "__main__":
    print("EPIS Nightly Recalculation -- Manuel Baslatma")
    print("=" * 55)

    nr     = NightlyRecalculation()
    report = nr.run_backlog()

    print(f"\nDurum   : {report['status']}")
    print(f"Asamalar: {report['stages']}")

    if report.get("errors"):
        print(f"\nHatalar: {report['errors']}")
    if report.get("highlights"):
        print("\nOzet:")
        for h in report["highlights"]:
            print(f"  - {h}")
    if report.get("epis_voice"):
        print(f"\nEPIS sesi:\n  {report['epis_voice']}")
    if report.get("tomorrow_context"):
        print(f"\nYarin: {report['tomorrow_context']}")

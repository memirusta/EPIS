import json
import logging
import os
from api_clients import Layer3APIClient
from privacy import PrivacyFilter


logging.basicConfig(level=logging.INFO, format='%(asctime)s - LAYER 2 - %(message)s')


class EpisRouter:
    def __init__(self):
        # Layer-3: Claude anahtari yoksa Gemini (keys.env LAYER3_* ile degistir)
        _l3 = os.getenv("LAYER3_GEMINI_FALLBACK", "gemini-3.5-flash")
        self.routing_table = {
            "code_writing":          os.getenv("LAYER3_CODE_MODEL", _l3),
            "visual_analysis":       os.getenv("LAYER3_VISUAL_MODEL", _l3),
            "math_reasoning":        os.getenv("LAYER3_MATH_MODEL", _l3),
            "deep_analysis":         os.getenv("LAYER3_DEEP_MODEL", _l3),
            "fast_tasks":            os.getenv("LAYER3_FAST_MODEL", _l3),
            "nightly_recalculation": "hybrid_pipeline",
            "hybrid_stage_1":        os.getenv("NIGHTLY_STAGE1_MODEL", "qwen2.5:14b"),
            "hybrid_stage_2":        os.getenv("NIGHTLY_STAGE2_MODEL", "qwen2.5:32b"),
        }

        self.privacy    = PrivacyFilter()
        self.api_client = Layer3APIClient()

    def intercept_tool_call(self, tool_call_data: dict, current_context: dict) -> dict:
        task_type   = tool_call_data.get("task_type", "unknown_task")
        raw_payload = tool_call_data.get("payload", "")

        logging.info(f"Layer 1'den Tool Call yakalandı: {task_type}")

        # Anonimleştir (stabil takma adlar) + yerel geri-çevirme haritasını al
        safe_payload, pseudonym_map = self.privacy.anonymize(raw_payload)
        target = self.routing_table.get(task_type, "gemini-3.5-flash")

        if target == "hybrid_pipeline":
            logging.info("Hybrid (Melez) is hatti baslatiliyor...")
            layer3_response = self._run_hybrid_pipeline(safe_payload, current_context)
        else:
            logging.info(f"Gorev '{task_type}' -> {target} modeline yonlendiriliyor...")
            layer3_response = self._dispatch_to_layer3(target, safe_payload, current_context)

        # Layer-3 yanıtındaki takma adları yerelde gerçek adlara çevir
        layer3_response = self.privacy.deanonymize(layer3_response, pseudonym_map)

        return {
            "status":     "success",
            "model_used": target,
            "result":     layer3_response,
        }

    def _apply_privacy_layer(self, payload) -> str:
        """Geriye dönük uyumlu sarmalayıcı -- yalnızca anonim metni döner (harita gerekmeyen yerler)."""
        safe, _ = self.privacy.anonymize(payload)
        return safe

    def _run_hybrid_pipeline(self, payload: str, context: dict) -> str:
        stage1 = self.routing_table.get("hybrid_stage_1", "gemini-3.5-flash")
        stage2 = self.routing_table.get("hybrid_stage_2", "claude-opus-4-8")

        logging.info(f"Hybrid Asama 1 -> {stage1}: veri ozeti...")
        gemini_summary = self._dispatch_to_layer3(stage1, payload, context)

        logging.info(f"Hybrid Asama 2 -> {stage2}: davranissal analiz...")
        claude_analysis = self._dispatch_to_layer3(stage2, gemini_summary, context)

        return claude_analysis

    def _dispatch_to_layer3(self, model: str, payload: str, context: dict) -> str:
        context_str  = json.dumps(context, ensure_ascii=False) if context else "{}"
        final_prompt = f"GOREV BAGLAMI:\n{context_str}\n\nUYGULANACAK GOREV:\n{payload}"

        logging.info(f"Layer 3 API cagrisi tetikleniyor. Hedef Model: {model}")

        response = self.api_client.execute_task(target_model=model, payload=final_prompt)
        return response

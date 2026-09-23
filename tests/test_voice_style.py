from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Layer-2" / "src"))

from agentic.voice import (  # noqa: E402
    build_voice_guidance,
    classify_voice,
    find_style_violations,
)


class VoiceClassifierTests(unittest.TestCase):
    def test_casual_short_turn_stays_short(self):
        profile = classify_voice("naber")
        self.assertEqual(profile.mode, "casual")
        self.assertEqual(profile.brevity, "short")

    def test_excited_turn_is_high_energy_without_forcing_mode(self):
        profile = classify_voice("CUUUUŞ OLDU LAN!!!")
        self.assertEqual(profile.mode, "casual")
        self.assertEqual(profile.energy, "high")

    def test_technical_turn_is_detected(self):
        profile = classify_voice("pytest yine fail veriyor, traceback burada")
        self.assertEqual(profile.mode, "technical")
        self.assertTrue(profile.technical)

    def test_frustrated_technical_turn_keeps_technical_focus(self):
        profile = classify_voice("amk build yine patladı, npm hata veriyor")
        self.assertEqual(profile.mode, "technical")
        self.assertEqual(profile.energy, "frustrated")

    def test_serious_turn_overrides_casual_energy(self):
        profile = classify_voice("Bu dosyaları silmeden önce ciddi şekilde kontrol et")
        self.assertEqual(profile.mode, "serious")

    def test_supportive_turn_is_calm(self):
        profile = classify_voice("çok kötü hissediyorum ve panik oldum")
        self.assertEqual(profile.mode, "supportive")
        self.assertEqual(profile.energy, "calm")


class VoiceGuidanceTests(unittest.TestCase):
    def test_guidance_explicitly_blocks_customer_service_default(self):
        guidance = build_voice_guidance("naber")
        self.assertIn("Müşteri hizmetleri/asistan dili kullanma", guidance)
        self.assertIn("'Elbette'", guidance)
        self.assertIn("argoyu mekanik biçimde kopyalama", guidance)
        self.assertIn("1-3 kısa cümleyi geçme", guidance)

    def test_technical_guidance_leads_with_diagnosis(self):
        guidance = build_voice_guidance("pytest fail veriyor")
        self.assertIn("sonucu/teşhisi başa koy", guidance)
        self.assertIn("rahat ama kesin ol", guidance)

    def test_serious_guidance_disables_jokes(self):
        guidance = build_voice_guidance("Bu kritik veriyi sil")
        self.assertIn("şaka, gevşek filler ve coşkulu ton kullanma", guidance)


class GoldenStyleLintTests(unittest.TestCase):
    def test_rejects_robotic_casual_reply(self):
        profile = classify_voice("ses 42 olsun")
        problems = find_style_violations(
            "Elbette. İşlem başarıyla tamamlandı. Size yardımcı olabilirim.",
            profile,
        )
        self.assertTrue(any(p.startswith("robotic_phrase:") for p in problems))

    def test_accepts_natural_action_reply(self):
        profile = classify_voice("ses 42 olsun")
        self.assertEqual(
            find_style_violations("Tamam, 42'ye çektim.", profile),
            [],
        )

    def test_serious_reply_rejects_jokey_emoji(self):
        profile = classify_voice("Bu kritik dosyayı sil")
        self.assertIn(
            "serious_mode_jokey_emoji",
            find_style_violations("Tamamdır 😂 önce silelim.", profile),
        )


class ProductionWiringTests(unittest.TestCase):
    def test_agent_core_injects_turn_voice_guidance(self):
        core = (ROOT / "Layer-2" / "src" / "agentic" / "core.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("from .voice import build_voice_guidance", core)
        self.assertIn('system += "\\n\\n" + build_voice_guidance(user_message)', core)

    def test_identity_voice_keeps_security_separate_from_style(self):
        voice = (ROOT / "Layer-1" / "identity" / "epis_voice.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Voice yalnızca doğru içeriğin", voice)
        self.assertIn("Security ve authorization davranışı sınırlar; sesi belirlemez", voice)
        self.assertIn("müşteri hizmetleri", voice.casefold())


if __name__ == "__main__":
    unittest.main()

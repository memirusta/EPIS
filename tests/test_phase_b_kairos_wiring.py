from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PhaseBKairosWiringTests(unittest.TestCase):
    def test_kairos_catches_up_memory_backlog_on_start_and_after_midnight(self):
        source = (ROOT / "Layer-2" / "src" / "kairos.py").read_text(encoding="utf-8")
        self.assertIn("NightlyRecalculation().run_backlog(sensor_data=sensor_data)", source)
        self.assertIn('schedule.every().day.at("00:15").do(self._trigger_nightly)', source)
        self.assertIn("# Memory v1: catch up completed transcript days when this trusted node starts.", source)
        self.assertIn("self._trigger_nightly()", source)


if __name__ == "__main__":
    unittest.main()

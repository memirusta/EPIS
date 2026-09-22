import sys
from pathlib import Path
import unittest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"),
)

from agentic.desktop import AppCatalog


class AppNameMatchingRegressionTests(unittest.TestCase):
    def test_chatgpt_desktop_resolves_to_chatgpt(self):
        catalog = AppCatalog(
            sources=(),
            start_sources=(
                lambda: [
                    ("ChatGPT", "ChatGPT"),
                    ("Sample Desktop Apps", "SampleDesktopApps"),
                    (
                        "Remote Desktop Connection",
                        "RemoteDesktopConnection",
                    ),
                ],
            ),
        )

        result = catalog.discover(
            {"query": "ChatGPT Desktop"}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            [item["app_name"] for item in result["apps"]],
            ["ChatGPT"],
        )


if __name__ == "__main__":
    unittest.main()

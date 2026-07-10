"""
اختبار دخان لهيكل FastAPI (اليوم الرابع): يتحقق من أن الخادم يبدأ بدون
ANTHROPIC_API_KEY (بدل الانهيار)، وأن /health يعكس ذلك بصدق، وأن نقاط التصحيح
تُرجع 503 واضحاً بدل محاولة استدعاء LM غير مُهيَّأ. لا يستدعي أي LM حقيقي.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class TestApiWithoutLm(unittest.TestCase):
    @patch.dict("os.environ", {}, clear=True)
    @patch("medical_ocr.lm_config.load_dotenv", lambda: None)
    def test_health_reports_lm_not_configured(self):
        from medical_ocr.api.app import app

        with TestClient(app) as client:
            response = client.get("/health")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["lm_configured"])

    @patch.dict("os.environ", {}, clear=True)
    @patch("medical_ocr.lm_config.load_dotenv", lambda: None)
    def test_correct_spelling_returns_503_without_lm(self):
        from medical_ocr.api.app import app

        with TestClient(app) as client:
            response = client.post("/correct-spelling", json={"raw_text": "مريض يعاني من سكرى"})
            self.assertEqual(response.status_code, 503)

    @patch.dict("os.environ", {}, clear=True)
    @patch("medical_ocr.lm_config.load_dotenv", lambda: None)
    def test_structure_table_returns_503_without_lm(self):
        from medical_ocr.api.app import app

        with TestClient(app) as client:
            response = client.post(
                "/structure-table",
                json={"raw_rows": [["A", "B"], ["1", "2"]], "column_hints": ["A", "B"]},
            )
            self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()

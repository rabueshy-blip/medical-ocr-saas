"""
اختبار دخان لـ medical_ocr.lm_config: يتحقق فقط من رسالة الخطأ الواضحة عند غياب
المفتاح، دون استدعاء أي LM حقيقي (لا يوجد مفتاح API في بيئة الاختبار).
"""

import unittest
from unittest.mock import patch

from medical_ocr.lm_config import configure_lm


class TestConfigureLmWithoutKey(unittest.TestCase):
    @patch.dict("os.environ", {}, clear=True)
    @patch("medical_ocr.lm_config.load_dotenv", lambda: None)
    def test_raises_clear_error_when_api_key_missing(self):
        with self.assertRaises(RuntimeError) as ctx:
            configure_lm()
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

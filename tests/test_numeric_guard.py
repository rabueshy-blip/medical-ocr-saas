"""اختبار دخان لـ medical_ocr.numeric_guard: استخراج الأرقام الصريحة الصرفة فقط،
مع استثناء عمدي لرموز التباس OCR (رقم ملتصق بحرف)."""

import unittest

from medical_ocr.numeric_guard import extract_pure_numbers


class TestExtractPureNumbers(unittest.TestCase):
    def test_extracts_plain_integers_and_decimals(self):
        self.assertEqual(
            extract_pure_numbers("الجرعة 850 ملغ والقيمة 0.9 طبيعية"),
            ["850", "0.9"],
        )

    def test_ignores_digit_letter_confusion_tokens(self):
        # "2O" و"5oo" ليست أرقاماً صرفة (حرف ملتصق) — لا تُستخرَج.
        self.assertEqual(extract_pure_numbers("جرعة 2O ملغ و5oo ملغ"), [])

    def test_returns_empty_list_for_text_without_numbers(self):
        self.assertEqual(extract_pure_numbers("لا يوجد أي رقم هنا"), [])

    def test_repeated_number_counted_each_occurrence(self):
        self.assertEqual(extract_pure_numbers("20 ملغ ثم 20 ملغ أخرى"), ["20", "20"])


if __name__ == "__main__":
    unittest.main()

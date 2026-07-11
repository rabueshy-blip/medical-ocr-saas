"""
اختبارات دخان للـ Signatures/الموديولات — لا تستدعي أي LM حقيقي (لا يوجد مفتاح API
في بيئة الاختبار). تُغطّي فقط: بنية الحقول، وخطوة Retrieve، والقيود البرمجية
الصرفة (is_correction_grounded / row_count_preserved).
"""

import json
import unittest

from medical_ocr.signatures.spelling import (
    MedicalSpellingCorrection,
    MedicalSpellingCorrector,
    is_correction_grounded,
    numeric_tokens_preserved,
    retrieve_candidate_terms,
)
from medical_ocr.signatures.tables import (
    MedicalTableStructuring,
    MedicalTableStructurer,
    row_count_preserved,
    row_values_grounded,
)
from medical_ocr.terminology import MedicalTerminologyRetriever, TermEntry


class TestMedicalSpellingCorrectionSignature(unittest.TestCase):
    def test_signature_has_expected_fields(self):
        fields = MedicalSpellingCorrection.model_fields
        self.assertIn("raw_text", fields)
        self.assertIn("candidate_terms", fields)
        self.assertIn("corrected_text", fields)
        self.assertIn("corrections", fields)
        self.assertIn("uncertain_terms", fields)

    def test_module_constructs_without_lm_configured(self):
        # بناء الموديول لا يستدعي أي LM؛ الاستدعاء الفعلي فقط عبر forward().
        corrector = MedicalSpellingCorrector()
        self.assertIsInstance(corrector, MedicalSpellingCorrector)


class TestRetrieveCandidateTerms(unittest.TestCase):
    def test_returns_empty_list_json_when_no_terminology(self):
        self.assertEqual(retrieve_candidate_terms("أي نص", None), "[]")

    def test_finds_candidate_for_misspelled_word(self):
        terminology = MedicalTerminologyRetriever([TermEntry(term="ميتفورمين", term_type="drug")])
        result = json.loads(retrieve_candidate_terms("المريض يتناول ميتفورمن يومياً", terminology))
        terms = [m["term"] for m in result]
        self.assertIn("ميتفورمين", terms)


class TestIsCorrectionGrounded(unittest.TestCase):
    def test_minor_spelling_fix_is_grounded(self):
        self.assertTrue(is_correction_grounded("مريض يعاني من سكرى", "مريض يعاني من سكري"))

    def test_full_rewrite_is_rejected(self):
        self.assertFalse(
            is_correction_grounded(
                "مريض يعاني من سكرى",
                "تم تشخيص الحالة بارتفاع حاد جداً في نسبة السكر في الدم مع مضاعفات كلوية",
            )
        )

    def test_empty_raw_text_is_trivially_grounded(self):
        self.assertTrue(is_correction_grounded("", ""))

    def test_changed_clear_dose_is_rejected(self):
        # "850" رقم صريح صرف، تغييره إلى "580" هلوسة خطيرة رغم بقاء التشابه
        # النصي الكلي مرتفعاً (فرق حرف واحد في نص قصير).
        self.assertFalse(
            is_correction_grounded(
                "يتناول المريض أتورفاستاتين 850 ملغ مساءً",
                "يتناول المريض أتورفاستاتين 580 ملغ مساءً",
            )
        )

    def test_digit_letter_confusion_correction_still_grounded(self):
        # "2O" ليس رقماً صرفاً في raw_text، فتصحيحه إلى "20" مسموح وليس هلوسة.
        self.assertTrue(
            is_correction_grounded(
                "يُنصح بتناول أوموبرازول 2O ملغ مرة يومياً",
                "يُنصح بتناول أوميبرازول 20 ملغ مرة يومياً",
            )
        )


class TestNumericTokensPreserved(unittest.TestCase):
    def test_identical_numbers_pass(self):
        self.assertTrue(numeric_tokens_preserved("جرعة 850 ملغ", "جرعة 850 ملغ مساءً"))

    def test_changed_number_fails(self):
        self.assertFalse(numeric_tokens_preserved("جرعة 850 ملغ", "جرعة 580 ملغ"))

    def test_dropped_number_fails(self):
        self.assertFalse(numeric_tokens_preserved("جرعة 850 ملغ", "جرعة ملغ"))

    def test_ocr_digit_letter_token_not_required_verbatim(self):
        # "2O" ليس رقماً صرفاً، فلا يُشترط ظهوره كما هو — تصحيحه إلى "20" سليم.
        self.assertTrue(numeric_tokens_preserved("جرعة 2O ملغ", "جرعة 20 ملغ"))


class TestMedicalTableStructuringSignature(unittest.TestCase):
    def test_signature_has_expected_fields(self):
        fields = MedicalTableStructuring.model_fields
        self.assertIn("raw_rows", fields)
        self.assertIn("column_hints", fields)
        self.assertIn("structured_rows", fields)
        self.assertIn("notes", fields)

    def test_module_constructs_without_lm_configured(self):
        structurer = MedicalTableStructurer()
        self.assertIsInstance(structurer, MedicalTableStructurer)


class TestRowCountPreserved(unittest.TestCase):
    def test_matching_row_count_passes(self):
        raw_rows = [["الدواء", "الجرعة"], ["باراسيتامول", "500 ملغ"]]
        structured = json.dumps([{"الدواء": "الدواء", "الجرعة": "الجرعة"}, {"الدواء": "باراسيتامول", "الجرعة": "500 ملغ"}])
        self.assertTrue(row_count_preserved(raw_rows, structured))

    def test_dropped_row_fails(self):
        raw_rows = [["A", "B"], ["C", "D"]]
        structured = json.dumps([{"A": "A", "B": "B"}])
        self.assertFalse(row_count_preserved(raw_rows, structured))

    def test_invalid_json_fails(self):
        self.assertFalse(row_count_preserved([["A"]], "not json"))


class TestRowValuesGrounded(unittest.TestCase):
    def test_preserved_lab_value_passes(self):
        raw_rows = [["Hemoglobin", "13.5"]]
        structured = json.dumps([{"الفحص": "Hemoglobin", "القيمة": "13.5"}])
        self.assertTrue(row_values_grounded(raw_rows, structured))

    def test_changed_lab_value_fails(self):
        raw_rows = [["Hemoglobin", "13.5"]]
        structured = json.dumps([{"الفحص": "Hemoglobin", "القيمة": "15.3"}])
        self.assertFalse(row_values_grounded(raw_rows, structured))

    def test_row_marked_uncertain_is_exempt_from_numeric_check(self):
        raw_rows = [["Glucose", "70"]]
        structured = json.dumps([{"الفحص": "Glucose", "القيمة": "UNCERTAIN"}])
        self.assertTrue(row_values_grounded(raw_rows, structured))

    def test_dropped_row_still_fails_via_row_count(self):
        raw_rows = [["A", "1"], ["B", "2"]]
        structured = json.dumps([{"A": "A", "1": "1"}])
        self.assertFalse(row_values_grounded(raw_rows, structured))

    def test_none_cell_in_raw_row_does_not_crash(self):
        # خلية فارغة (None) في raw_rows شائعة قبل التطبيع (انظر schema.py) — يجب
        # ألا تُسبب استثناءً غير معالَج داخل reward_fn الخاص بـ dspy.Refine.
        raw_rows = [["Hemoglobin", None]]
        structured = json.dumps([{"الفحص": "Hemoglobin", "القيمة": "13.5"}])
        self.assertTrue(row_values_grounded(raw_rows, structured))


if __name__ == "__main__":
    unittest.main()

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
    retrieve_candidate_terms,
)
from medical_ocr.signatures.tables import (
    MedicalTableStructuring,
    MedicalTableStructurer,
    row_count_preserved,
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


if __name__ == "__main__":
    unittest.main()

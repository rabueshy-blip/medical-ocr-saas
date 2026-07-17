"""
اختبارات دخان للـ Signatures/الموديولات — لا تستدعي أي LM حقيقي (لا يوجد مفتاح API
في بيئة الاختبار). تُغطّي فقط: بنية الحقول، وخطوة Retrieve، والقيود البرمجية
الصرفة (is_correction_grounded / row_count_preserved).
"""

import json
import unittest

import dspy

from medical_ocr.schema import Block, BlockCategory, BlockType, Page, SourceEngine
from medical_ocr.signatures.classification import (
    MedicalBlockClassification,
    MedicalBlockClassifier,
    apply_classification_to_page,
    build_page_blocks_payload,
    classification_reward,
    encode_page_blocks,
    is_classification_valid,
)
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


class TestMedicalBlockClassificationSignature(unittest.TestCase):
    def test_signature_has_expected_fields(self):
        fields = MedicalBlockClassification.model_fields
        self.assertIn("page_blocks", fields)
        self.assertIn("classifications", fields)

    def test_module_constructs_without_lm_configured(self):
        classifier = MedicalBlockClassifier()
        self.assertIsInstance(classifier, MedicalBlockClassifier)


class TestBuildPageBlocksPayload(unittest.TestCase):
    def _page(self):
        return Page(
            page_number=1,
            source="digital",
            blocks=[
                Block(
                    block_type=BlockType.PARAGRAPH,
                    text="اسم المريض: أحمد محمد",
                    source_engine=SourceEngine.PYMUPDF,
                ),
                Block(
                    block_type=BlockType.TABLE,
                    rows=[["Hemoglobin", "13.5"], ["Glucose", "95"]],
                    source_engine=SourceEngine.PDFPLUMBER,
                ),
            ],
        )

    def test_payload_shape_is_json_round_trippable(self):
        payload = build_page_blocks_payload(self._page())
        encoded = encode_page_blocks(payload)
        decoded = json.loads(encoded)
        self.assertEqual(len(decoded), 2)
        self.assertEqual(decoded[0]["index"], 0)
        self.assertEqual(decoded[0]["block_type"], "paragraph")
        self.assertIn("أحمد محمد", decoded[0]["preview"])
        self.assertEqual(decoded[1]["block_type"], "table")
        self.assertIn("Hemoglobin", decoded[1]["preview"])

    def test_prefers_corrected_text_over_raw_when_available(self):
        page = self._page()
        corrections = {"p1_b0": {"corrected_text": "اسم المريض: أحمد محمد علي"}}
        payload = build_page_blocks_payload(page, corrections)
        self.assertIn("أحمد محمد علي", payload[0]["preview"])

    def test_prefers_structured_rows_over_raw_for_tables(self):
        page = self._page()
        corrections = {"p1_b1": {"structured_rows": [{"الفحص": "Hemoglobin", "القيمة": "13.5"}]}}
        payload = build_page_blocks_payload(page, corrections)
        self.assertIn("Hemoglobin", payload[1]["preview"])


class TestIsClassificationValid(unittest.TestCase):
    def _blocks_json(self, n):
        return encode_page_blocks([{"index": i, "block_type": "paragraph", "preview": "x"} for i in range(n)])

    def test_valid_full_coverage_passes(self):
        classifications = json.dumps(
            [
                {"index": 0, "category": "patient_info"},
                {"index": 1, "category": "clinical_results"},
                {"index": 2, "category": "doctor_notes"},
            ]
        )
        self.assertTrue(is_classification_valid(self._blocks_json(3), classifications))

    def test_missing_index_fails(self):
        classifications = json.dumps([{"index": 0, "category": "other"}, {"index": 1, "category": "other"}])
        self.assertFalse(is_classification_valid(self._blocks_json(3), classifications))

    def test_duplicate_index_fails(self):
        classifications = json.dumps(
            [
                {"index": 0, "category": "other"},
                {"index": 0, "category": "other"},
                {"index": 1, "category": "other"},
            ]
        )
        self.assertFalse(is_classification_valid(self._blocks_json(3), classifications))

    def test_out_of_range_index_fails(self):
        classifications = json.dumps([{"index": 0, "category": "other"}, {"index": 5, "category": "other"}])
        self.assertFalse(is_classification_valid(self._blocks_json(2), classifications))

    def test_invalid_category_value_fails(self):
        classifications = json.dumps([{"index": 0, "category": "lab_results"}])
        self.assertFalse(is_classification_valid(self._blocks_json(1), classifications))

    def test_invalid_json_fails(self):
        self.assertFalse(is_classification_valid(self._blocks_json(1), "not json"))

    def test_non_list_classifications_fails(self):
        self.assertFalse(is_classification_valid(self._blocks_json(1), json.dumps({"0": "other"})))

    def test_missing_required_key_in_item_fails(self):
        classifications = json.dumps([{"index": 0}])
        self.assertFalse(is_classification_valid(self._blocks_json(1), classifications))


class TestClassificationReward(unittest.TestCase):
    def test_reward_is_one_for_valid_prediction(self):
        page_blocks_json = encode_page_blocks([{"index": 0, "block_type": "paragraph", "preview": "x"}])
        prediction = dspy.Prediction(classifications=json.dumps([{"index": 0, "category": "other"}]))
        self.assertEqual(classification_reward({"page_blocks": page_blocks_json}, prediction), 1.0)

    def test_reward_is_zero_for_invalid_prediction(self):
        page_blocks_json = encode_page_blocks([{"index": 0, "block_type": "paragraph", "preview": "x"}])
        prediction = dspy.Prediction(classifications="not json")
        self.assertEqual(classification_reward({"page_blocks": page_blocks_json}, prediction), 0.0)


class TestApplyClassificationToPage(unittest.TestCase):
    def _page(self):
        return Page(
            page_number=1,
            source="digital",
            blocks=[
                Block(block_type=BlockType.PARAGRAPH, text="a", source_engine=SourceEngine.PYMUPDF),
                Block(block_type=BlockType.PARAGRAPH, text="b", source_engine=SourceEngine.PYMUPDF),
            ],
        )

    def test_valid_prediction_sets_category_on_each_block(self):
        page = self._page()
        page_blocks = [
            {"index": 0, "block_type": "paragraph", "preview": "a"},
            {"index": 1, "block_type": "paragraph", "preview": "b"},
        ]
        prediction = dspy.Prediction(
            classifications=json.dumps(
                [{"index": 0, "category": "patient_info"}, {"index": 1, "category": "doctor_notes"}]
            )
        )
        result = apply_classification_to_page(page, page_blocks, prediction)
        self.assertTrue(result)
        self.assertEqual(page.blocks[0].category, BlockCategory.PATIENT_INFO)
        self.assertEqual(page.blocks[1].category, BlockCategory.DOCTOR_NOTES)

    def test_invalid_prediction_falls_back_to_other_for_whole_page_and_returns_false(self):
        page = self._page()
        page_blocks = [
            {"index": 0, "block_type": "paragraph", "preview": "a"},
            {"index": 1, "block_type": "paragraph", "preview": "b"},
        ]
        prediction = dspy.Prediction(classifications="not json")
        result = apply_classification_to_page(page, page_blocks, prediction)
        self.assertFalse(result)
        self.assertEqual(page.blocks[0].category, BlockCategory.OTHER)
        self.assertEqual(page.blocks[1].category, BlockCategory.OTHER)


if __name__ == "__main__":
    unittest.main()

"""
اختبار دخان لـ medical_ocr.eval_cases: يتحقق فقط من بنية حالات الاختبار الصعبة
وأن الكلمة الملتبسة تُطابق فعلاً مصطلحين مختلفين في القاموس المرجعي (شرط أساسي
لصحة حالة drug_name_ambiguity)، دون استدعاء أي LM.
"""

import unittest
from pathlib import Path

from medical_ocr.eval_cases import TABLE_CASES, TERMINOLOGY_CASES
from medical_ocr.signatures.spelling import retrieve_candidate_terms
from medical_ocr.terminology import MedicalTerminologyRetriever

TERMS_PATH = Path(__file__).resolve().parent.parent / "data" / "medical_terms_sample.txt"


class TestTerminologyCases(unittest.TestCase):
    def test_cases_are_non_empty(self):
        self.assertTrue(len(TERMINOLOGY_CASES) >= 2)
        for case in TERMINOLOGY_CASES:
            self.assertTrue(case.raw_text.strip())

    def test_drug_name_ambiguity_matches_two_distinct_drugs(self):
        terminology = MedicalTerminologyRetriever.from_file(TERMS_PATH)
        case = next(c for c in TERMINOLOGY_CASES if c.name == "drug_name_ambiguity")
        import json

        candidates = json.loads(retrieve_candidate_terms(case.raw_text, terminology))
        matched_terms = {c["term"] for c in candidates}
        self.assertIn("ميتفورمين", matched_terms)
        self.assertIn("ميتوبرولول", matched_terms)


class TestTableCases(unittest.TestCase):
    def test_multi_level_header_case_preserves_row_shape(self):
        case = next(c for c in TABLE_CASES if c.name == "multi_level_lab_header")
        row_lengths = {len(row) for row in case.raw_rows}
        self.assertEqual(row_lengths, {len(case.column_hints)})


if __name__ == "__main__":
    unittest.main()

"""
اختبار دخان لـ medical_ocr.gold_dataset: يتحقق من بنية الـ Gold Dataset (15 عيّنة:
10 مصطلحات + 5 جداول) وتحويلها إلى dspy.Example، دون استدعاء أي LM.
"""

import unittest

from medical_ocr.gold_dataset import (
    build_table_devset,
    build_terminology_devset,
    load_gold_dataset,
)


class TestGoldDataset(unittest.TestCase):
    def setUp(self):
        self.gold = load_gold_dataset()

    def test_total_size_is_fifteen(self):
        self.assertEqual(len(self.gold.terminology), 10)
        self.assertEqual(len(self.gold.tables), 5)
        self.assertEqual(self.gold.size, 15)

    def test_terminology_examples_have_non_empty_raw_text(self):
        for example in self.gold.terminology:
            self.assertTrue(example.raw_text.strip())
            self.assertTrue(example.id)

    def test_table_examples_have_consistent_row_shape(self):
        for example in self.gold.tables:
            row_lengths = {len(row) for row in example.raw_rows}
            self.assertEqual(len(row_lengths), 1, f"صفوف غير متساوية الطول في {example.id}")

    def test_table_expected_row_values_reference_valid_indices(self):
        for example in self.gold.tables:
            row_count = len(example.raw_rows)
            for row_index_str in example.expected_row_values:
                self.assertLess(int(row_index_str), row_count, f"فهرس صف غير صالح في {example.id}")
            for row_index in example.expected_uncertain_row_indices:
                self.assertLess(row_index, row_count, f"فهرس صف غير صالح في {example.id}")

    def test_drug_ambiguity_case_has_forbidden_term(self):
        case = next(e for e in self.gold.terminology if e.id == "drug_name_ambiguity_metoprolol")
        self.assertIn("ميتفورمين", case.forbidden_terms)
        self.assertIn("ميتوبرولول", case.expected_correction_terms)


class TestGoldDevsetBuilders(unittest.TestCase):
    def setUp(self):
        self.gold = load_gold_dataset()

    def test_terminology_devset_marks_raw_text_as_input(self):
        devset = build_terminology_devset(self.gold)
        self.assertEqual(len(devset), 10)
        example = devset[0]
        self.assertEqual(set(example.inputs().keys()), {"raw_text"})

    def test_table_devset_marks_raw_rows_and_column_hints_as_input(self):
        devset = build_table_devset(self.gold)
        self.assertEqual(len(devset), 5)
        example = devset[0]
        self.assertEqual(set(example.inputs().keys()), {"raw_rows", "column_hints"})


if __name__ == "__main__":
    unittest.main()

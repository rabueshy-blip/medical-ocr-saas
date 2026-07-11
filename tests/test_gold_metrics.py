"""
اختبار دخان لـ medical_ocr.gold_metrics: يبني dspy.Example/dspy.Prediction اصطناعية
يدوياً (بدون تشغيل أي موديول DSPy حقيقي أو LM) للتحقق من منطق القياس نفسه:
بوابة منع الهلوسة أولاً، ثم الدرجة الجزئية لمطابقة المصطلحات/الخلايا المتوقعة.
"""

import unittest

import dspy

from medical_ocr.gold_metrics import table_metric, terminology_metric


def _terminology_example(**overrides):
    fields = dict(
        raw_text="يُعطى المريض ميتفوبرولين 50 ملغ مرتين يومياً",
        expected_correction_terms=["ميتوبرولول"],
        forbidden_terms=["ميتفورمين"],
        expected_uncertain_terms=[],
    )
    fields.update(overrides)
    return dspy.Example(**fields).with_inputs("raw_text")


class TestTerminologyMetric(unittest.TestCase):
    def test_perfect_correction_scores_high(self):
        example = _terminology_example()
        prediction = dspy.Prediction(
            corrected_text="يُعطى المريض ميتوبرولول 50 ملغ مرتين يومياً",
            corrections="[]",
            uncertain_terms="[]",
        )
        self.assertGreaterEqual(terminology_metric(example, prediction), 0.9)

    def test_forbidden_term_scores_zero(self):
        example = _terminology_example()
        prediction = dspy.Prediction(
            corrected_text="يُعطى المريض ميتفورمين 50 ملغ مرتين يومياً",
            corrections="[]",
            uncertain_terms="[]",
        )
        self.assertEqual(terminology_metric(example, prediction), 0.0)

    def test_non_grounded_rewrite_scores_zero(self):
        example = _terminology_example()
        prediction = dspy.Prediction(
            corrected_text="نص مختلف تماماً لا علاقة له بالنص الأصلي بتاتاً ومُعاد صياغته بالكامل من الصفر",
            corrections="[]",
            uncertain_terms="[]",
        )
        self.assertEqual(terminology_metric(example, prediction), 0.0)

    def test_hallucinated_dosage_number_scores_zero(self):
        # الرقم 50 صريح وصحيح في raw_text؛ تغييره إلى 500 هلوسة جرعة خطيرة يجب
        # أن تُفشل الدرجة كاملة رغم بقاء بقية النص شبه مطابق.
        example = _terminology_example()
        prediction = dspy.Prediction(
            corrected_text="يُعطى المريض ميتوبرولول 500 ملغ مرتين يومياً",
            corrections="[]",
            uncertain_terms="[]",
        )
        self.assertEqual(terminology_metric(example, prediction), 0.0)

    def test_uncertain_term_correctly_flagged_scores_high(self):
        example = _terminology_example(
            expected_correction_terms=[],
            forbidden_terms=[],
            expected_uncertain_terms=["xxxxرول"],
            raw_text="المريض يتناول xxxxرول 40 ملغ صباحاً",
        )
        prediction = dspy.Prediction(
            corrected_text="المريض يتناول xxxxرول 40 ملغ صباحاً",
            corrections="[]",
            uncertain_terms='["xxxxرول"]',
        )
        self.assertGreaterEqual(terminology_metric(example, prediction), 0.9)


def _table_example(**overrides):
    fields = dict(
        raw_rows=[["الدواء", "الجرعة"], ["أموكسيسيلين", "500 ملغ"]],
        column_hints=["الدواء", "الجرعة"],
        expected_row_values={"1": ["أموكسيسيلين", "500 ملغ"]},
        expected_uncertain_row_indices=[],
    )
    fields.update(overrides)
    return dspy.Example(**fields).with_inputs("raw_rows", "column_hints")


class TestTableMetric(unittest.TestCase):
    def test_matching_structured_rows_scores_high(self):
        example = _table_example()
        prediction = dspy.Prediction(
            structured_rows='[{"الدواء": "الدواء", "الجرعة": "الجرعة"}, '
            '{"الدواء": "أموكسيسيلين", "الجرعة": "500 ملغ"}]',
            notes="[]",
        )
        self.assertGreaterEqual(table_metric(example, prediction), 0.9)

    def test_dropped_row_scores_zero(self):
        example = _table_example()
        prediction = dspy.Prediction(
            structured_rows='[{"الدواء": "أموكسيسيلين", "الجرعة": "500 ملغ"}]',
            notes="[]",
        )
        self.assertEqual(table_metric(example, prediction), 0.0)

    def test_hallucinated_lab_value_scores_zero(self):
        # "500 ملغ" في raw_rows تحوّلت إلى "50 ملغ" في الصف المُهيكَل دون أن
        # يُعلَّم الصف UNCERTAIN — تغيير جرعة صريحة يجب أن يُفشل الدرجة كاملة.
        example = _table_example()
        prediction = dspy.Prediction(
            structured_rows='[{"الدواء": "الدواء", "الجرعة": "الجرعة"}, '
            '{"الدواء": "أموكسيسيلين", "الجرعة": "50 ملغ"}]',
            notes="[]",
        )
        self.assertEqual(table_metric(example, prediction), 0.0)

    def test_missing_uncertain_flag_scores_zero(self):
        example = _table_example(
            expected_row_values={},
            expected_uncertain_row_indices=[1],
        )
        prediction = dspy.Prediction(
            structured_rows='[{"الدواء": "الدواء", "الجرعة": "الجرعة"}, '
            '{"الدواء": "أموكسيسيلين", "الجرعة": "0"}]',
            notes="[]",
        )
        self.assertEqual(table_metric(example, prediction), 0.0)


if __name__ == "__main__":
    unittest.main()

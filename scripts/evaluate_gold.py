"""
اليوم الخامس (plan.md): قياس دقة موديولات DSPy (MedicalSpellingCorrector /
MedicalTableStructurer) كمّياً مقابل Gold Dataset (data/gold/)، بدل الاكتفاء
بالمراجعة اليدوية لحقل reasoning كما في اليوم الرابع (scripts/run_hard_cases.py).

يستخدم dspy.Evaluate مع دوال القياس في medical_ocr/gold_metrics.py (بوابة منع
هلوسة صارمة أولاً، ثم درجة جزئية بحسب مطابقة المصطلحات/الخلايا المتوقعة).

الاستخدام:
    GEMINI_API_KEY=... .venv/bin/python scripts/evaluate_gold.py
أو ضع المفتاح في ملف .env في جذر المشروع.

يطبع متوسط الدرجة لكل مجموعة (مصطلحات/جداول) ولكل عيّنة على حدة، ويحفظ تفصيلاً
كاملاً (raw/prediction/score) إلى scripts/gold_eval_results.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dspy

from medical_ocr.gold_dataset import build_table_devset, build_terminology_devset, load_gold_dataset
from medical_ocr.gold_metrics import table_metric, terminology_metric
from medical_ocr.lm_config import configure_lm
from medical_ocr.signatures.spelling import MedicalSpellingCorrector
from medical_ocr.signatures.tables import MedicalTableStructurer
from medical_ocr.terminology import DEFAULT_TERMS_PATH, MedicalTerminologyRetriever

RESULTS_PATH = Path(__file__).resolve().parent / "gold_eval_results.json"


def evaluate_terminology(corrector: MedicalSpellingCorrector) -> tuple:
    devset = build_terminology_devset(load_gold_dataset())
    evaluator = dspy.Evaluate(
        devset=devset,
        metric=terminology_metric,
        display_progress=True,
        display_table=False,
        return_outputs=True,
    )
    avg_score, per_example = evaluator(corrector)

    per_example_report = []
    for example, prediction, score in per_example:
        print(f"\n=== [terminology] {example.raw_text[:60]!r} -> score={score:.2f} ===")
        per_example_report.append(
            {
                "raw_text": example.raw_text,
                "expected_correction_terms": example.expected_correction_terms,
                "forbidden_terms": example.forbidden_terms,
                "expected_uncertain_terms": example.expected_uncertain_terms,
                "corrected_text": getattr(prediction, "corrected_text", None),
                "uncertain_terms": getattr(prediction, "uncertain_terms", None),
                "score": score,
            }
        )
    return avg_score, per_example_report


def evaluate_tables(structurer: MedicalTableStructurer) -> tuple:
    devset = build_table_devset(load_gold_dataset())
    evaluator = dspy.Evaluate(
        devset=devset,
        metric=table_metric,
        display_progress=True,
        display_table=False,
        return_outputs=True,
    )
    avg_score, per_example = evaluator(structurer)

    per_example_report = []
    for example, prediction, score in per_example:
        print(f"\n=== [table] {len(example.raw_rows)} rows -> score={score:.2f} ===")
        per_example_report.append(
            {
                "raw_rows": example.raw_rows,
                "column_hints": example.column_hints,
                "expected_row_values": example.expected_row_values,
                "expected_uncertain_row_indices": example.expected_uncertain_row_indices,
                "structured_rows": getattr(prediction, "structured_rows", None),
                "notes": getattr(prediction, "notes", None),
                "score": score,
            }
        )
    return avg_score, per_example_report


def main() -> None:
    configure_lm()
    terminology = MedicalTerminologyRetriever.from_file(DEFAULT_TERMS_PATH)
    corrector = MedicalSpellingCorrector(terminology=terminology)
    structurer = MedicalTableStructurer()

    terminology_score, terminology_report = evaluate_terminology(corrector)
    table_score, table_report = evaluate_tables(structurer)

    print("\n" + "=" * 60)
    print(f"متوسط درجة المصطلحات: {terminology_score:.2f} / 100")
    print(f"متوسط درجة الجداول:   {table_score:.2f} / 100")

    RESULTS_PATH.write_text(
        json.dumps(
            {
                "terminology_score": terminology_score,
                "table_score": table_score,
                "terminology_results": terminology_report,
                "table_results": table_report,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nنتائج كاملة محفوظة في: {RESULTS_PATH}")


if __name__ == "__main__":
    main()

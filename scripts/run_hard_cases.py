"""
اليوم الرابع (plan.md): أول تشغيل فعلي لموديولات DSPy (CoT) ضد LM حقيقي، على
حالات "صعبة" عمداً (medical_ocr/eval_cases.py) — لقياس دقة الاستخراج بدل
الاكتفاء ببنية الموديول.

الاستخدام:
    ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python scripts/run_hard_cases.py
أو ضع المفتاح في ملف .env في جذر المشروع.

يطبع لكل حالة: النص/الجدول الخام، حقل reasoning (تفكير CoT الفعلي)، والمخرجات،
ويحفظ نسخة JSON كاملة تحت scripts/hard_case_results.json لغرض التدقيق.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medical_ocr.eval_cases import TABLE_CASES, TERMINOLOGY_CASES
from medical_ocr.lm_config import configure_lm
from medical_ocr.signatures.spelling import MedicalSpellingCorrector
from medical_ocr.signatures.tables import MedicalTableStructurer
from medical_ocr.terminology import MedicalTerminologyRetriever

TERMS_PATH = Path(__file__).resolve().parent.parent / "data" / "medical_terms_sample.txt"
RESULTS_PATH = Path(__file__).resolve().parent / "hard_case_results.json"


def run_terminology_cases(corrector: MedicalSpellingCorrector) -> list:
    results = []
    for case in TERMINOLOGY_CASES:
        prediction = corrector(raw_text=case.raw_text)
        print(f"\n=== [terminology] {case.name} ===")
        print(f"raw_text: {case.raw_text}")
        print(f"note: {case.note}")
        print(f"reasoning: {prediction.reasoning}")
        print(f"corrected_text: {prediction.corrected_text}")
        print(f"corrections: {prediction.corrections}")
        print(f"uncertain_terms: {prediction.uncertain_terms}")
        results.append(
            {
                "case": case.name,
                "raw_text": case.raw_text,
                "reasoning": prediction.reasoning,
                "corrected_text": prediction.corrected_text,
                "corrections": prediction.corrections,
                "uncertain_terms": prediction.uncertain_terms,
            }
        )
    return results


def run_table_cases(structurer: MedicalTableStructurer) -> list:
    results = []
    for case in TABLE_CASES:
        prediction = structurer(raw_rows=case.raw_rows, column_hints=case.column_hints)
        print(f"\n=== [table] {case.name} ===")
        print(f"raw_rows: {case.raw_rows}")
        print(f"note: {case.note}")
        print(f"reasoning: {prediction.reasoning}")
        print(f"structured_rows: {prediction.structured_rows}")
        print(f"notes: {prediction.notes}")
        results.append(
            {
                "case": case.name,
                "raw_rows": case.raw_rows,
                "reasoning": prediction.reasoning,
                "structured_rows": prediction.structured_rows,
                "notes": prediction.notes,
            }
        )
    return results


def main() -> None:
    configure_lm()
    terminology = MedicalTerminologyRetriever.from_file(TERMS_PATH)
    corrector = MedicalSpellingCorrector(terminology=terminology)
    structurer = MedicalTableStructurer()

    all_results = {
        "terminology_cases": run_terminology_cases(corrector),
        "table_cases": run_table_cases(structurer),
    }
    RESULTS_PATH.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nنتائج كاملة محفوظة في: {RESULTS_PATH}")


if __name__ == "__main__":
    main()

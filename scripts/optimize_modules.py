"""
اليوم الخامس (plan.md): بناء مُحسِّن DSPy (Optimizer) يستخدم Gold Dataset
(data/gold/) كأمثلة تدريب حقيقية لتوليد demos تلقائياً للموديولين
MedicalSpellingCorrector وMedicalTableStructurer، بدل الاعتماد فقط على تعليمات
الـ Signature النصية.

يستخدم `dspy.teleprompt.BootstrapFewShot` (وليس MIPROv2): مع 15 عيّنة فقط
إجمالاً، BootstrapFewShot أنسب لأنه لا يحتاج ميزانية تجارب/تقييمات كبيرة مثل
MIPROv2 (الذي يستهدف أساساً مجموعات بيانات أكبر). كل مجموعة (مصطلحات/جداول)
تُقسَّم train/dev: يُبنى الموديل المُحسَّن من train، ثم يُقاس قبل/بعد على dev
عبر نفس دوال medical_ocr/gold_metrics المستخدمة في scripts/evaluate_gold.py،
لتفادي أي تسريب بيانات (data leakage) بين التدريب والتقييم.

الاستخدام:
    ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python scripts/optimize_modules.py
أو ضع المفتاح في ملف .env في جذر المشروع.

يحفظ حالة الموديولين المُحسَّنين (demos فقط، JSON) تحت scripts/optimized/، ليتم
تحميلها لاحقاً عبر `module.load(path)` بدل إعادة التحسين في كل تشغيل.
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

OPTIMIZED_DIR = Path(__file__).resolve().parent / "optimized"
RESULTS_PATH = Path(__file__).resolve().parent / "optimize_results.json"

METRIC_THRESHOLD = 0.8
TERMINOLOGY_TRAIN_SIZE = 7
TABLE_TRAIN_SIZE = 3


def _score_devset(program: dspy.Module, devset: list, metric) -> float:
    evaluator = dspy.Evaluate(devset=devset, metric=metric, display_progress=False, display_table=False)
    return evaluator(program)


def optimize_terminology() -> dict:
    terminology = MedicalTerminologyRetriever.from_file(DEFAULT_TERMS_PATH)
    devset_all = build_terminology_devset(load_gold_dataset())
    trainset, devset = devset_all[:TERMINOLOGY_TRAIN_SIZE], devset_all[TERMINOLOGY_TRAIN_SIZE:]

    baseline = MedicalSpellingCorrector(terminology=terminology)
    baseline_score = _score_devset(baseline, devset, terminology_metric)

    optimizer = dspy.teleprompt.BootstrapFewShot(
        metric=terminology_metric,
        metric_threshold=METRIC_THRESHOLD,
        max_bootstrapped_demos=3,
        max_labeled_demos=3,
        max_rounds=1,
    )
    student = MedicalSpellingCorrector(terminology=terminology)
    optimized = optimizer.compile(student=student, trainset=trainset)
    optimized_score = _score_devset(optimized, devset, terminology_metric)

    OPTIMIZED_DIR.mkdir(parents=True, exist_ok=True)
    optimized.save(str(OPTIMIZED_DIR / "spelling_corrector.json"))

    print(f"\n[terminology] baseline dev score: {baseline_score:.2f} / 100")
    print(f"[terminology] optimized dev score: {optimized_score:.2f} / 100")
    return {
        "train_size": len(trainset),
        "dev_size": len(devset),
        "baseline_dev_score": baseline_score,
        "optimized_dev_score": optimized_score,
    }


def optimize_tables() -> dict:
    devset_all = build_table_devset(load_gold_dataset())
    trainset, devset = devset_all[:TABLE_TRAIN_SIZE], devset_all[TABLE_TRAIN_SIZE:]

    baseline = MedicalTableStructurer()
    baseline_score = _score_devset(baseline, devset, table_metric)

    optimizer = dspy.teleprompt.BootstrapFewShot(
        metric=table_metric,
        metric_threshold=METRIC_THRESHOLD,
        max_bootstrapped_demos=2,
        max_labeled_demos=2,
        max_rounds=1,
    )
    student = MedicalTableStructurer()
    optimized = optimizer.compile(student=student, trainset=trainset)
    optimized_score = _score_devset(optimized, devset, table_metric)

    OPTIMIZED_DIR.mkdir(parents=True, exist_ok=True)
    optimized.save(str(OPTIMIZED_DIR / "table_structurer.json"))

    print(f"\n[tables] baseline dev score: {baseline_score:.2f} / 100")
    print(f"[tables] optimized dev score: {optimized_score:.2f} / 100")
    return {
        "train_size": len(trainset),
        "dev_size": len(devset),
        "baseline_dev_score": baseline_score,
        "optimized_dev_score": optimized_score,
    }


def main() -> None:
    configure_lm()
    terminology_report = optimize_terminology()
    table_report = optimize_tables()

    RESULTS_PATH.write_text(
        json.dumps(
            {"terminology": terminology_report, "tables": table_report},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nنتائج التحسين محفوظة في: {RESULTS_PATH}")
    print(f"الموديولات المُحسَّنة محفوظة في: {OPTIMIZED_DIR}/")


if __name__ == "__main__":
    main()

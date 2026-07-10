"""
Signature الأول (plan.md القسم 6): تصحيح واستخراج المصطلحات الطبية، وتصحيح
الأخطاء الإملائية الناتجة عن OCR — كموديول DSPy بدلاً من prompt نصي تقليدي.

المبدأ الأساسي لمنع الهلوسة (القسم 2) منعكس هنا برمجياً عبر:
- خطوة Retrieve (`retrieve_candidate_terms`) تجلب مصطلحات مرشحة من قاموس مرجعي
  محلي عبر fuzzy matching *قبل* استدعاء الموديل، بدل الاعتماد على معرفته الداخلية.
- قيد ترسيخ برمجي (`is_correction_grounded`) مطبَّق عبر `dspy.Refine` (يعيد
  المحاولة حتى N مرات بحرارة مختلفة ويختار أول نتيجة تتجاوز threshold) يرفض أي
  تصحيح يبتعد كثيراً عن النص الخام — دفاع برمجي فعلي، وليس مجرد تعليمة نصية.
  ملاحظة: `dspy.Suggest`/`dspy.Assert` المذكورة في القسم 6 من الخطة أُزيلت من
  إصدار dspy المُثبَّت هنا (2.6.27) واستُبدلت بـ `dspy.Refine` كآلية القيد الحالية.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import dspy
from rapidfuzz import fuzz

from ..terminology import MedicalTerminologyRetriever

_WORD_RE = re.compile(r"[\w؀-ۿ]+", re.UNICODE)


class MedicalSpellingCorrection(dspy.Signature):
    """صحّح الأخطاء الإملائية والمصطلحات الطبية في نص مستخرج عبر OCR.

    قواعد صارمة:
    - عدّل فقط الكلمات المشكوك في صحتها إملائياً أو المصطلحات الطبية المعروفة.
    - ممنوع توليد أو إضافة أي معلومة طبية غير موجودة أصلاً في raw_text.
    - استخدم candidate_terms (نتائج بحث تشابه من قاموس مصطلحات مرجعي) كمرجع
      أساسي للتصحيح، ولا تخترع مصطلحات من خارجها.
    - إن لم تكن متأكداً من التصحيح الصحيح لكلمة ما، أبقها كما هي في corrected_text
      وأضفها إلى uncertain_terms بدلاً من التخمين.
    """

    raw_text: str = dspy.InputField(
        desc="النص الخام كما استخرجه محرك الـ OCR، قد يحتوي أخطاء إملائية أو التباس حروف"
    )
    candidate_terms: str = dspy.InputField(
        desc="JSON: مصطلحات طبية مرشحة من بحث fuzzy matching في القاموس المرجعي، للاسترشاد بها فقط"
    )
    corrected_text: str = dspy.OutputField(
        desc="النص بعد التصحيح الإملائي/المصطلحي فقط، بدون أي إضافة محتوى جديد"
    )
    corrections: str = dspy.OutputField(
        desc="JSON: قائمة {original, corrected, term_type} لكل تصحيح تم تطبيقه، لغرض التتبع (audit trail)"
    )
    uncertain_terms: str = dspy.OutputField(
        desc="JSON: قائمة الكلمات التي لم يُبتّ في تصحيحها بثقة كافية وتُركت كما هي"
    )


def retrieve_candidate_terms(raw_text: str, terminology: Optional[MedicalTerminologyRetriever]) -> str:
    """خطوة Retrieve قبل موديول DSPy (القسم 6): fuzzy matching في القاموس المرجعي."""
    if terminology is None:
        return "[]"
    seen = set()
    matches = []
    for word in _WORD_RE.findall(raw_text):
        for match in terminology.suggest(word):
            key = (word, match.term)
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                {"word": word, "term": match.term, "term_type": match.term_type, "score": match.score}
            )
    return json.dumps(matches, ensure_ascii=False)


def is_correction_grounded(
    raw_text: str,
    corrected_text: str,
    min_char_similarity: float = 55.0,
    max_extra_words: int = 2,
) -> bool:
    """قيد ترسيخ برمجي: يرفض تصحيحاً يعيد صياغة النص كلياً أو يضيف كلمات كثيرة جديدة."""
    if not raw_text.strip():
        return True
    char_similarity = fuzz.ratio(raw_text, corrected_text)
    word_count_delta = len(corrected_text.split()) - len(raw_text.split())
    return char_similarity >= min_char_similarity and word_count_delta <= max_extra_words


def spelling_grounding_reward(call_kwargs: dict, prediction: dspy.Prediction) -> float:
    """reward_fn لـ dspy.Refine: 1.0 إن كان التصحيح مُرتكِزاً على النص الخام، وإلا 0.0."""
    return 1.0 if is_correction_grounded(call_kwargs["raw_text"], prediction.corrected_text) else 0.0


class MedicalSpellingCorrector(dspy.Module):
    """موديول DSPy الذي يغلّف MedicalSpellingCorrection بخطوة Retrieve وقيد ترسيخ عبر dspy.Refine."""

    def __init__(self, terminology: Optional[MedicalTerminologyRetriever] = None, max_attempts: int = 3):
        super().__init__()
        self.terminology = terminology
        base = dspy.ChainOfThought(MedicalSpellingCorrection)
        self.correct = dspy.Refine(
            module=base,
            N=max_attempts,
            reward_fn=spelling_grounding_reward,
            threshold=1.0,
        )

    def forward(self, raw_text: str) -> dspy.Prediction:
        candidate_terms = retrieve_candidate_terms(raw_text, self.terminology)
        return self.correct(raw_text=raw_text, candidate_terms=candidate_terms)

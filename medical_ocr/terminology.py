"""
خطوة الترسيخ (Grounding) الخارجي — راجع plan.md القسم 6:

"ترسيخ تصحيح الإملاء الطبي بمرجع خارجي (قاموس مصطلحات طبية / قائمة أدوية
وتشخيصات محلية) عبر بحث تشابه (fuzzy matching) كخطوة Retrieve قبل موديول DSPy."

هذه الوحدة توفر Retrieve بسيط قائم على rapidfuzz فوق ملف قاموس محلي (نص عادي،
سطر لكل مصطلح بصيغة `term,term_type`). القاموس الحالي في data/medical_terms_sample.txt
هو عيّنة مؤقتة فقط — يُستبدل لاحقاً بمرجع رسمي حسب اللغة المستهدفة (القسم 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Union

from rapidfuzz import fuzz, process


@dataclass(frozen=True)
class TermEntry:
    term: str
    term_type: str


@dataclass(frozen=True)
class TermMatch:
    term: str
    term_type: str
    score: float


class MedicalTerminologyRetriever:
    """يبحث عن أقرب المصطلحات الطبية المرجعية لكلمة معطاة عبر fuzzy matching."""

    def __init__(self, entries: List[TermEntry]):
        self._entries = entries
        self._choices = [entry.term for entry in entries]

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "MedicalTerminologyRetriever":
        entries: List[TermEntry] = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",", 1)
            term = parts[0].strip()
            term_type = parts[1].strip() if len(parts) > 1 else "unknown"
            if term:
                entries.append(TermEntry(term=term, term_type=term_type))
        return cls(entries)

    def suggest(self, word: str, limit: int = 3, score_cutoff: float = 70.0) -> List[TermMatch]:
        if not word or not self._choices:
            return []
        matches = process.extract(
            word, self._choices, scorer=fuzz.WRatio, limit=limit, score_cutoff=score_cutoff
        )
        return [
            TermMatch(term=self._entries[idx].term, term_type=self._entries[idx].term_type, score=float(score))
            for _choice, score, idx in matches
        ]

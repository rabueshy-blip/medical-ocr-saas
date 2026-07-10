"""اعتماديات FastAPI (Depends): موديولات DSPy مبنية مرة واحدة فقط (singletons عبر
lru_cache) بدل إعادة تحميل قاموس المصطلحات أو إعادة بناء الموديول في كل طلب."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..signatures.spelling import MedicalSpellingCorrector
from ..signatures.tables import MedicalTableStructurer
from ..terminology import MedicalTerminologyRetriever

TERMS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "medical_terms_sample.txt"


@lru_cache(maxsize=1)
def get_terminology() -> MedicalTerminologyRetriever:
    return MedicalTerminologyRetriever.from_file(TERMS_PATH)


@lru_cache(maxsize=1)
def get_spelling_corrector() -> MedicalSpellingCorrector:
    return MedicalSpellingCorrector(terminology=get_terminology())


@lru_cache(maxsize=1)
def get_table_structurer() -> MedicalTableStructurer:
    return MedicalTableStructurer()

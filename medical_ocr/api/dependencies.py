"""اعتماديات FastAPI (Depends): موديولات DSPy مبنية مرة واحدة فقط (singletons عبر
lru_cache) بدل إعادة تحميل قاموس المصطلحات أو إعادة بناء الموديول في كل طلب."""

from __future__ import annotations

from functools import lru_cache

from ..signatures.spelling import MedicalSpellingCorrector
from ..signatures.tables import MedicalTableStructurer
from ..terminology import DEFAULT_TERMS_PATH, MedicalTerminologyRetriever


@lru_cache(maxsize=1)
def get_terminology() -> MedicalTerminologyRetriever:
    return MedicalTerminologyRetriever.from_file(DEFAULT_TERMS_PATH)


@lru_cache(maxsize=1)
def get_spelling_corrector() -> MedicalSpellingCorrector:
    return MedicalSpellingCorrector(terminology=get_terminology())


@lru_cache(maxsize=1)
def get_table_structurer() -> MedicalTableStructurer:
    return MedicalTableStructurer()

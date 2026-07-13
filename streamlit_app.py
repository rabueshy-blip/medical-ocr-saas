"""
واجهة Streamlit بسيطة: رفع ملف PDF طبي ومشاهدة نتيجة الاستخراج مباشرة في المتصفح.

خط الأنابيب المستخدم (`medical_ocr/ingest.py`): نص رقمي مباشر عبر PyMuPDF + جداول
pdfplumber للصفحات التي تحوي طبقة نص، وOCR عبر easyocr للصفحات الممسوحة ضوئياً.

مبدأ مهم يعكس حداً بيئياً حقيقياً (راجع الذاكرة/plan.md): حصة Gemini المجانية صغيرة
جداً وتُستهلك بسرعة، لذلك لا يوجد "تصحيح تلقائي للكل" — كل تصحيح بالذكاء الاصطناعي
(تصحيح إملائي أو هيكلة جدول) زر منفصل يضغطه المستخدم عمداً لكل مقطع/جدول، ليتحكم
بنفسه في استهلاك الحصة اليومية.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile

import streamlit as st

from medical_ocr.api.dependencies import get_spelling_corrector, get_table_structurer
from medical_ocr.ingest import extract_document
from medical_ocr.lm_config import DEFAULT_MODEL, configure_lm
from medical_ocr.schema import BlockType, PageSource, SourceEngine

st.set_page_config(page_title="أداة الـ OCR الطبية", page_icon="🩺", layout="wide")


@st.cache_resource(show_spinner=False)
def _configure_lm_once() -> dict:
    try:
        configure_lm()
        return {"configured": True, "error": None}
    except RuntimeError as exc:
        return {"configured": False, "error": str(exc)}


def _file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _try_parse_json(raw: str):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


PAGE_SOURCE_LABELS = {PageSource.DIGITAL: "رقمية (نص مباشر)", PageSource.SCANNED: "ممسوحة (OCR)"}

lm_status = _configure_lm_once()

with st.sidebar:
    st.header("حالة الذكاء الاصطناعي")
    if lm_status["configured"]:
        model_name = os.getenv("MEDICAL_OCR_LM_MODEL", DEFAULT_MODEL)
        st.success(f"متصل — النموذج: {model_name}")
    else:
        st.error("غير متصل: لا يوجد GEMINI_API_KEY")
        st.caption(lm_status["error"])
    st.info(
        "تنبيه: حصة Gemini المجانية محدودة جداً يومياً، لذلك التصحيح بالذكاء "
        "الاصطناعي اختياري لكل نص/جدول عبر زر مخصص، وليس تلقائياً للملف كاملاً."
    )

st.title("🩺 أداة الـ OCR الطبية")
st.caption("ارفع ملف PDF طبي (رقمي أو ممسوح ضوئياً) لاستخراج نصه وجداوله، مع تصحيح اختياري بالذكاء الاصطناعي.")

uploaded_file = st.file_uploader("ارفع ملف PDF", type=["pdf"])

if uploaded_file is not None:
    file_bytes = uploaded_file.getvalue()
    file_hash = _file_hash(file_bytes)

    if st.session_state.get("file_hash") != file_hash:
        with st.spinner("جارٍ استخراج النص من الملف... قد يستغرق وقتاً أطول للصفحات الممسوحة ضوئياً"):
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
                tmp_file.write(file_bytes)
                tmp_path = tmp_file.name
            try:
                document = extract_document(tmp_path, file_name=uploaded_file.name)
            except Exception as exc:  # ملف تالف/ليس PDF فعلياً رغم الامتداد، إلخ
                st.session_state["file_hash"] = file_hash
                st.session_state["document"] = None
                st.session_state["corrections"] = {}
                st.error(f"تعذّرت قراءة الملف كـ PDF صالح: {exc}")
                document = None
            else:
                st.session_state["file_hash"] = file_hash
                st.session_state["document"] = document
                st.session_state["corrections"] = {}
            finally:
                os.unlink(tmp_path)
    else:
        document = st.session_state["document"]

    if document is None:
        st.stop()
    digital_pages = sum(1 for p in document.pages if p.source == PageSource.DIGITAL)
    scanned_pages = len(document.pages) - digital_pages
    st.success(f"تم الاستخراج: {len(document.pages)} صفحة ({digital_pages} رقمية، {scanned_pages} ممسوحة)")

    for page in document.pages:
        with st.expander(f"صفحة {page.page_number} — {PAGE_SOURCE_LABELS[page.source]}", expanded=True):
            if not page.blocks:
                st.caption("لم يُستخرج أي محتوى من هذه الصفحة.")
                continue

            for block_index, block in enumerate(page.blocks):
                block_key = f"p{page.page_number}_b{block_index}"

                if block.block_type == BlockType.TABLE:
                    st.markdown("**جدول**")
                    st.table(block.rows)

                    if lm_status["configured"]:
                        hints_input = st.text_input(
                            "أسماء أعمدة متوقعة (اختياري، مفصولة بفاصلة)",
                            key=f"{block_key}_hints",
                        )
                        if st.button("هيكلة الجدول بالذكاء الاصطناعي", key=f"{block_key}_table_btn"):
                            column_hints = [h.strip() for h in hints_input.split(",") if h.strip()] or None
                            with st.spinner("جارٍ هيكلة الجدول..."):
                                try:
                                    prediction = get_table_structurer()(raw_rows=block.rows, column_hints=column_hints)
                                    st.session_state["corrections"][block_key] = {
                                        "structured_rows": _try_parse_json(prediction.structured_rows),
                                        "notes": _try_parse_json(prediction.notes),
                                        "reasoning": prediction.reasoning,
                                    }
                                except Exception as exc:  # فشل استدعاء LM (شبكة/حصة/نموذج)
                                    st.error(f"فشل استدعاء الذكاء الاصطناعي: {exc}")

                    result = st.session_state.get("corrections", {}).get(block_key)
                    if result:
                        st.markdown("**الجدول بعد الهيكلة:**")
                        st.json(result["structured_rows"])
                        if result["notes"]:
                            st.markdown("**ملاحظات:**")
                            st.json(result["notes"])
                        with st.expander("تفكير النموذج (Reasoning)"):
                            st.write(result["reasoning"])

                else:
                    engine_label = "OCR" if block.source_engine == SourceEngine.EASYOCR else "نص رقمي"
                    st.markdown(f"**فقرة** _(المصدر: {engine_label}, ثقة: {block.confidence:.2f})_")
                    st.text_area("النص الخام", value=block.text, height=100, key=f"{block_key}_raw", disabled=True)

                    if lm_status["configured"] and block.source_engine == SourceEngine.EASYOCR:
                        if st.button("صحّح هذا النص بالذكاء الاصطناعي", key=f"{block_key}_spell_btn"):
                            with st.spinner("جارٍ التصحيح..."):
                                try:
                                    prediction = get_spelling_corrector()(raw_text=block.text)
                                    st.session_state["corrections"][block_key] = {
                                        "corrected_text": prediction.corrected_text,
                                        "corrections": _try_parse_json(prediction.corrections),
                                        "uncertain_terms": _try_parse_json(prediction.uncertain_terms),
                                        "reasoning": prediction.reasoning,
                                    }
                                except Exception as exc:
                                    st.error(f"فشل استدعاء الذكاء الاصطناعي: {exc}")

                    result = st.session_state.get("corrections", {}).get(block_key)
                    if result:
                        st.text_area(
                            "النص بعد التصحيح", value=result["corrected_text"], height=100, key=f"{block_key}_corrected", disabled=True
                        )
                        col1, col2 = st.columns(2)
                        with col1:
                            st.markdown("**التصحيحات المطبَّقة:**")
                            st.json(result["corrections"])
                        with col2:
                            st.markdown("**كلمات غير مؤكدة:**")
                            st.json(result["uncertain_terms"])
                        with st.expander("تفكير النموذج (Reasoning)"):
                            st.write(result["reasoning"])

                st.divider()
else:
    st.info("ارفع ملف PDF أعلاه للبدء.")

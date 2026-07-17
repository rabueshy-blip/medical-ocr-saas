"""
واجهة Streamlit: رفع ملف PDF طبي، استخراج نصه وجداوله، تصنيف اختياري بالذكاء
الاصطناعي إلى فئات (معلومات مريض/نتائج سريرية/ملاحظات طبيب)، عرض مبوَّب حسب
الفئة + عرض "جنباً إلى جنب" مع الصفحة الأصلية، وتصدير تقارير (Word/Excel/CSV).

خط الأنابيب المستخدم (`medical_ocr/ingest.py`): نص رقمي مباشر عبر PyMuPDF + جداول
pdfplumber للصفحات التي تحوي طبقة نص، وOCR عبر Google Vision API للصفحات الممسوحة ضوئياً
(يتطلب GOOGLE_VISION_API_KEY، انظر .env.example).

مبدأ مهم يعكس حداً بيئياً حقيقياً (راجع الذاكرة/plan.md): حصة Gemini المجانية صغيرة
جداً وتُستهلك بسرعة، لذلك لا يوجد "تصحيح/تصنيف تلقائي للكل عند الرفع" — كل تصحيح
إملائي أو هيكلة جدول زر منفصل لكل مقطع، وتصنيف المستند بالكامل زر صريح واحد
("تصنيف المستند") يستدعي LM مرة واحدة **لكل صفحة** (وليس لكل مقطع)، كي يتحكم
المستخدم بنفسه في وقت/حجم استهلاك الحصة اليومية بدل تصنيف تلقائي مخفي التكلفة.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Optional

import fitz  # PyMuPDF
import streamlit as st

from medical_ocr.api.dependencies import get_block_classifier, get_spelling_corrector, get_table_structurer
from medical_ocr.ingest import extract_document
from medical_ocr.lm_config import DEFAULT_MODEL, configure_lm
from medical_ocr.reporting import (
    block_key,
    build_docx_report,
    build_tables_workbook_xlsx,
    build_tables_zip_csv,
    build_translation_ready_docx,
    table_block_to_dataframe,
)
from medical_ocr.schema import BlockCategory, BlockType, Document, PageSource, SourceEngine
from medical_ocr.signatures.classification import apply_classification_to_page, build_page_blocks_payload

st.set_page_config(page_title="أداة الـ OCR الطبية", page_icon="🩺", layout="wide")


def _load_secrets_into_env() -> None:
    """على Streamlit Community Cloud تُضبط المفاتيح عبر st.secrets (لوحة التحكم)، لا عبر
    .env محلي — هذه الدالة تجسرها إلى متغيرات البيئة كي يعمل configure_lm() (الذي يقرأ
    os.getenv فقط) دون أي تعديل عليه. لا تأثير محلياً حيث تُقرأ القيم أصلاً من .env."""
    try:
        secrets = st.secrets
        for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "MEDICAL_OCR_LM_MODEL", "GOOGLE_VISION_API_KEY"):
            if key not in os.environ and key in secrets:
                os.environ[key] = secrets[key]
    except Exception:
        return


_load_secrets_into_env()


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

CATEGORY_TABS = [
    (BlockCategory.PATIENT_INFO, "📋 معلومات المريض"),
    (BlockCategory.CLINICAL_RESULTS, "🧪 النتائج السريرية"),
    (BlockCategory.DOCTOR_NOTES, "📝 ملاحظات الطبيب"),
]
SIDE_BY_SIDE_TAB_LABEL = "🖼️ عرض جنباً إلى جنب"
OTHER_TAB_LABEL = "❔ غير مصنّف"

lm_status = _configure_lm_once()
vision_configured = bool(os.getenv("GOOGLE_VISION_API_KEY"))

with st.sidebar:
    st.header("حالة الذكاء الاصطناعي")
    if lm_status["configured"]:
        model_name = os.getenv("MEDICAL_OCR_LM_MODEL", DEFAULT_MODEL)
        st.success(f"متصل — النموذج: {model_name}")
    else:
        st.error("غير متصل: لا يوجد GEMINI_API_KEY")
        st.caption(lm_status["error"])
    st.info(
        "تنبيه: حصة Gemini المجانية محدودة جداً يومياً، لذلك التصحيح/التصنيف "
        "بالذكاء الاصطناعي اختياري عبر أزرار مخصَّصة، وليس تلقائياً عند الرفع."
    )

    st.header("حالة استخراج الصفحات الممسوحة")
    if vision_configured:
        st.success("متصل — Google Vision API (GOOGLE_VISION_API_KEY)")
    else:
        st.error("غير متصل: لا يوجد GOOGLE_VISION_API_KEY")
        st.caption("الصفحات الرقمية غير متأثرة؛ الصفحات الممسوحة ضوئياً ستظهر برسالة فشل استخراج لكل صفحة.")

st.title("🩺 أداة الـ OCR الطبية")
st.caption("ارفع ملف PDF طبي (رقمي أو ممسوح ضوئياً) لاستخراج نصه وجداوله، مع تصحيح/تصنيف اختياري بالذكاء الاصطناعي.")


def _render_block(page_number: int, block_index: int, block, key_suffix: str = "") -> None:
    """يعرض مقطعاً واحداً (فقرة أو جدول) مع أزرار التصحيح/الهيكلة الاختيارية —
    مُستخدَمة من كل من العرض المسطَّح (قبل التصنيف) وتبويبات الفئات والعرض
    جنباً إلى جنب، كي لا يتكرر منطق العرض بثلاث نسخ مختلفة.

    `key_suffix` يُميّز مفاتيح عناصر Streamlit فقط (وليس مفتاح قاموس corrections
    الذي يبقى بلا لاحقة) — لازم لأن st.tabs يُظهر كل التبويبات في نفس الجولة،
    فنفس المقطع قد يُعرَض في تبويب فئته وفي تبويب "جنباً إلى جنب" معاً، وبلا هذا
    التمييز يتصادم مفتاحا العنصرين (StreamlitDuplicateElementKey)."""
    store_key = block_key(page_number, block_index)
    widget_key = store_key + key_suffix

    if block.block_type == BlockType.TABLE:
        st.markdown("**جدول**")
        existing_result = st.session_state.get("corrections", {}).get(store_key)
        structured_rows = existing_result.get("structured_rows") if existing_result else None
        st.dataframe(table_block_to_dataframe(block, structured_rows), use_container_width=True)

        if lm_status["configured"]:
            hints_input = st.text_input(
                "أسماء أعمدة متوقعة (اختياري، مفصولة بفاصلة)",
                key=f"{widget_key}_hints",
            )
            if st.button("هيكلة الجدول بالذكاء الاصطناعي", key=f"{widget_key}_table_btn"):
                column_hints = [h.strip() for h in hints_input.split(",") if h.strip()] or None
                with st.spinner("جارٍ هيكلة الجدول..."):
                    try:
                        prediction = get_table_structurer()(raw_rows=block.rows, column_hints=column_hints)
                        st.session_state["corrections"][store_key] = {
                            "structured_rows": _try_parse_json(prediction.structured_rows),
                            "notes": _try_parse_json(prediction.notes),
                            "reasoning": prediction.reasoning,
                        }
                    except Exception as exc:  # فشل استدعاء LM (شبكة/حصة/نموذج)
                        st.error(f"فشل استدعاء الذكاء الاصطناعي: {exc}")

        result = st.session_state.get("corrections", {}).get(store_key)
        if result:
            st.markdown("**الجدول بعد الهيكلة:**")
            st.json(result["structured_rows"])
            if result["notes"]:
                st.markdown("**ملاحظات:**")
                st.json(result["notes"])
            with st.expander("تفكير النموذج (Reasoning)", key=f"{widget_key}_table_reasoning"):
                st.write(result["reasoning"])

    elif block.source_engine == SourceEngine.GOOGLE_VISION and block.confidence == 0.0:
        # Block placeholder فشل استخراج (انظر extract_document في ingest.py) —
        # ليس نصاً طبياً فعلياً، فلا يُعرَض كفقرة عادية ولا يُتاح له زر تصحيح.
        st.error(block.text)

    else:
        engine_label = "OCR (Google Vision)" if block.source_engine == SourceEngine.GOOGLE_VISION else "نص رقمي"
        st.markdown(f"**فقرة** _(المصدر: {engine_label}, ثقة: {block.confidence:.2f})_")
        st.text_area("النص الخام", value=block.text, height=100, key=f"{widget_key}_raw", disabled=True)

        if lm_status["configured"] and block.source_engine == SourceEngine.GOOGLE_VISION:
            if st.button("صحّح هذا النص بالذكاء الاصطناعي", key=f"{widget_key}_spell_btn"):
                with st.spinner("جارٍ التصحيح..."):
                    try:
                        prediction = get_spelling_corrector()(raw_text=block.text)
                        st.session_state["corrections"][store_key] = {
                            "corrected_text": prediction.corrected_text,
                            "corrections": _try_parse_json(prediction.corrections),
                            "uncertain_terms": _try_parse_json(prediction.uncertain_terms),
                            "reasoning": prediction.reasoning,
                        }
                    except Exception as exc:
                        st.error(f"فشل استدعاء الذكاء الاصطناعي: {exc}")

        result = st.session_state.get("corrections", {}).get(store_key)
        if result:
            st.text_area(
                "النص بعد التصحيح",
                value=result["corrected_text"],
                height=100,
                key=f"{widget_key}_corrected",
                disabled=True,
            )
            col1, col2 = st.columns(2)
            with col1:
                st.markdown("**التصحيحات المطبَّقة:**")
                st.json(result["corrections"])
            with col2:
                st.markdown("**كلمات غير مؤكدة:**")
                st.json(result["uncertain_terms"])
            with st.expander("تفكير النموذج (Reasoning)", key=f"{widget_key}_spell_reasoning"):
                st.write(result["reasoning"])

    st.divider()


def _render_flat_view(document: Document) -> None:
    """العرض الحالي قبل التصنيف: كل صفحة في expander منفصل، بترتيب الاستخراج."""
    for page in document.pages:
        with st.expander(f"صفحة {page.page_number} — {PAGE_SOURCE_LABELS[page.source]}", expanded=True):
            if not page.blocks:
                st.caption("لم يُستخرج أي محتوى من هذه الصفحة.")
                continue
            for block_index, block in enumerate(page.blocks):
                _render_block(page.page_number, block_index, block)


def _get_page_preview_png(page_number: int) -> Optional[bytes]:
    """يرستر صفحة واحدة من PDF الأصلي (المحفوظ في session_state كـ file_bytes) إلى
    PNG لعرضها في تبويب "جنباً إلى جنب"، مع تخزين مؤقت في session_state.

    التخزين المؤقت إلزامي وليس تحسيناً اختيارياً: st.tabs يُعيد رسم محتوى كل
    تبويب في كل rerun (وليس التبويب المرئي فقط)، فبدون هذا التخزين سيُعاد ترسير
    كل صفحة عبر fitz في كل تفاعل غير متعلق بها إطلاقاً (كتابة في حقل نصي مثلاً)."""
    cache = st.session_state.setdefault("page_preview_cache", {})
    if page_number not in cache:
        file_bytes = st.session_state.get("file_bytes")
        if not file_bytes:
            return None
        try:
            with fitz.open(stream=file_bytes, filetype="pdf") as pdf_doc:
                pixmap = pdf_doc[page_number - 1].get_pixmap(dpi=120)
                cache[page_number] = pixmap.tobytes("png")
        except Exception:
            cache[page_number] = None
    return cache[page_number]


def _render_classified_view(document: Document) -> None:
    """العرض بعد التصنيف: تبويب لكل فئة (تُستثنى "غير مصنّف" إن كانت فارغة) +
    تبويب أخير للعرض جنباً إلى جنب مع الصفحة الأصلية."""
    has_other = any(
        block.category == BlockCategory.OTHER for page in document.pages for block in page.blocks
    )
    tabs_spec = list(CATEGORY_TABS)
    if has_other:
        tabs_spec.append((BlockCategory.OTHER, OTHER_TAB_LABEL))

    tabs = st.tabs([label for _, label in tabs_spec] + [SIDE_BY_SIDE_TAB_LABEL])

    for (category, _), tab in zip(tabs_spec, tabs[:-1]):
        with tab:
            matching = [
                (page.page_number, index, block)
                for page in document.pages
                for index, block in enumerate(page.blocks)
                if block.category == category
            ]
            if not matching:
                st.caption("لا يوجد محتوى مصنَّف بهذه الفئة.")
            for page_number, index, block in matching:
                st.caption(f"من صفحة {page_number}")
                _render_block(page_number, index, block)

    with tabs[-1]:
        for page in document.pages:
            st.subheader(f"صفحة {page.page_number}")
            preview_col, content_col = st.columns(2)
            with preview_col:
                preview_png = _get_page_preview_png(page.page_number)
                if preview_png:
                    st.image(preview_png, use_container_width=True)
                else:
                    st.caption("تعذّر عرض معاينة هذه الصفحة.")
            with content_col:
                if not page.blocks:
                    st.caption("لم يُستخرج أي محتوى من هذه الصفحة.")
                for index, block in enumerate(page.blocks):
                    _render_block(page.page_number, index, block, key_suffix="_sbs")
            st.divider()


def _render_classify_button(document: Document) -> None:
    if st.session_state.get("document_classified"):
        return
    non_empty_pages = [page for page in document.pages if page.blocks]
    if not non_empty_pages:
        return

    if not lm_status["configured"]:
        return

    st.info(
        f"سيُستدعى الذكاء الاصطناعي مرة واحدة لكل صفحة بها محتوى "
        f"(حتى {len(non_empty_pages)} استدعاء) لتصنيف المحتوى إلى فئات."
    )
    if not st.button("🏷️ تصنيف المستند بالذكاء الاصطناعي"):
        return

    classifier = get_block_classifier()
    meta = {}
    progress = st.progress(0.0)
    for i, page in enumerate(non_empty_pages):
        payload = build_page_blocks_payload(page, st.session_state.get("corrections", {}))
        try:
            prediction = classifier(page_blocks=payload)
            ok = apply_classification_to_page(page, payload, prediction)
            meta[page.page_number] = {
                "status": "ok" if ok else "fallback",
                "reasoning": getattr(prediction, "reasoning", ""),
            }
        except Exception as exc:  # فشل استدعاء LM (شبكة/حصة/نموذج) — لا يوقف بقية الصفحات
            for block in page.blocks:
                block.category = BlockCategory.OTHER
            meta[page.page_number] = {"status": "fallback", "reasoning": str(exc)}
        progress.progress((i + 1) / len(non_empty_pages))

    st.session_state["classification_meta"] = meta
    st.session_state["document_classified"] = True
    st.rerun()


def _render_download_options(document: Document) -> None:
    with st.expander("⬇️ خيارات التنزيل", expanded=False):
        structured_tables = {
            key: value.get("structured_rows")
            for key, value in st.session_state.get("corrections", {}).items()
            if "structured_rows" in value
        }

        st.download_button(
            "📄 التقرير الكامل (Word)",
            data=build_docx_report(document, structured_tables=structured_tables),
            file_name="تقرير_كامل.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        if st.session_state.get("document_classified"):
            try:
                translation_bytes = build_translation_ready_docx(document, structured_tables=structured_tables)
            except ValueError:
                translation_bytes = None
            if translation_bytes:
                st.download_button(
                    "🌐 نص جاهز للترجمة (DOCX)",
                    data=translation_bytes,
                    file_name="نص_جاهز_للترجمة.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
        else:
            st.caption("شغّل تصنيف المستند أولاً لتفعيل تنزيل النص الجاهز للترجمة.")

        has_tables = any(block.block_type == BlockType.TABLE for page in document.pages for block in page.blocks)
        if has_tables:
            export_format = st.radio(
                "صيغة بيانات الجداول", ["Excel", "CSV"], horizontal=True, key="tables_export_format"
            )
            if export_format == "Excel":
                st.download_button(
                    "📊 بيانات الجداول (Excel)",
                    data=build_tables_workbook_xlsx(document, structured_tables=structured_tables),
                    file_name="جداول.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            else:
                st.download_button(
                    "📊 بيانات الجداول (CSV)",
                    data=build_tables_zip_csv(document, structured_tables=structured_tables),
                    file_name="جداول.zip",
                    mime="application/zip",
                )
        else:
            st.caption("لا توجد جداول في هذا المستند.")


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
                st.session_state["file_bytes"] = None
                st.session_state["document"] = None
                st.session_state["corrections"] = {}
                st.session_state["document_classified"] = False
                st.session_state["classification_meta"] = {}
                st.session_state["page_preview_cache"] = {}
                st.error(f"تعذّرت قراءة الملف كـ PDF صالح: {exc}")
                document = None
            else:
                st.session_state["file_hash"] = file_hash
                st.session_state["file_bytes"] = file_bytes
                st.session_state["document"] = document
                st.session_state["corrections"] = {}
                st.session_state["document_classified"] = False
                st.session_state["classification_meta"] = {}
                st.session_state["page_preview_cache"] = {}
            finally:
                os.unlink(tmp_path)
    else:
        document = st.session_state["document"]

    if document is None:
        st.stop()
    digital_pages = sum(1 for p in document.pages if p.source == PageSource.DIGITAL)
    scanned_pages = len(document.pages) - digital_pages
    st.success(f"تم الاستخراج: {len(document.pages)} صفحة ({digital_pages} رقمية، {scanned_pages} ممسوحة)")

    _render_classify_button(document)

    if st.session_state.get("document_classified"):
        _render_classified_view(document)
    else:
        _render_flat_view(document)

    _render_download_options(document)
else:
    st.info("ارفع ملف PDF أعلاه للبدء.")

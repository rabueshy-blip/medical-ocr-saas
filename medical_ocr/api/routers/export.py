"""تصدير محتوى المحرر (JSON من TipTap، بعد تحرير المترجم) إلى Word حقيقي أو PDF —
جداول كجداول حقيقية وليست نصاً مسطَّحاً. الصور **لا** تُضمَّن كبيانات حقيقية داخل Word
(انظر `_add_image`) — تُستبدَل بـplaceholder نصي، وتُسلَّم الصورة الفعلية في مجلد
`images/` ضمن ZIP عند وجود صور (`export_docx`).

يقبل المحتوى المُحرَّر من الواجهة مباشرة (وليس Document الأصلي من extract-document) عمداً:
الهدف تصدير النتيجة *بعد* ترجمة/تعديل المترجم، لا النص الخام المُستخرَج.

تصدير PDF عبر Playwright (Chromium headless) وليس WeasyPrint عمداً — WeasyPrint يحتاج
Pango/Cairo على مستوى النظام غير المتاحَين في هذه البيئة (لا Homebrew/sudo)، بينما ثنائي
Chromium الخاص بـ Playwright يُنزَّل عبر pip بلا صلاحيات إدارية."""

from __future__ import annotations

import base64
import html
import io
import logging
import re
import zipfile
from typing import List

from docx import Document as DocxDocument
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["export"])


class ExportImage(BaseModel):
    """صورة مُستخرَجة أصلاً عبر `extract-document` (`schema.ImageAsset`)، تُرسَل هنا فقط
    عند التصدير كي تُحزَم في مجلد `images/` داخل ملف ZIP النهائي — راجع توثيق
    `export_docx` أدناه لسبب عدم تضمين الصورة الحقيقية داخل Word نفسه."""

    image_id: str
    mime_type: str = "image/png"
    data_base64: str


class ExportRequest(BaseModel):
    content: dict
    file_name: str = "translated_document"
    images: List[ExportImage] = Field(default_factory=list)


def _safe_file_name(file_name: str) -> str:
    """يبقي فقط أحرف ASCII آمنة — `\\w` في Python يطابق أحرفاً عربية/يونيكود أيضاً
    (ليس ASCII فقط)، فيمر اسم ملف عربي دون تغيير ثم يُسبِّب `UnicodeEncodeError` عند
    وضعه في ترويسة `Content-Disposition` (يجب أن تكون latin-1 قابلة للترميز حسب
    HTTP). اسم الملف الأصلي (بأي لغة) يبقى محفوظاً في المستند نفسه، هذا فقط لاسم
    ملف التنزيل."""
    return re.sub(r"[^a-zA-Z0-9_\-. ]", "_", file_name).strip() or "translated_document"


def _extract_text(node: dict) -> str:
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(_extract_text(child) for child in node.get("content", []))


def _add_paragraph_or_heading(doc: DocxDocument, node: dict) -> None:
    text = _extract_text(node)
    if node["type"] == "heading":
        level = node.get("attrs", {}).get("level") or 1
        doc.add_heading(text, level=min(max(int(level), 1), 9))
    else:
        doc.add_paragraph(text)


def _add_table(doc: DocxDocument, node: dict) -> None:
    """يبني جدول Word حقيقياً — بما فيها دمج الخلايا (colspan) القادمة من TipTap
    (`attrs.colspan` على tableCell/tableHeader، مُولَّدة أصلاً من خلايا مدمجة حقيقية في
    PDF المصدر عبر documentToTiptap.ts، أو من دمج يدوي للمترجم داخل المحرر).

    عرض الشبكة الكلي (`num_cols`) يُحسَب من **مجموع** colspan لكل صف (وليس عدد عناصر
    المحتوى) لأن صفاً فيه خلية مدمجة يحتوي عناصر JSON أقل من عرض الشبكة الفعلي."""
    row_nodes = node.get("content", [])
    if not row_nodes:
        return
    num_cols = max(
        (
            sum(
                int(cell_node.get("attrs", {}).get("colspan", 1))
                for cell_node in row_node.get("content", [])
            )
            for row_node in row_nodes
        ),
        default=0,
    )
    if num_cols == 0:
        return
    table = doc.add_table(rows=0, cols=num_cols)
    table.style = "Table Grid"
    for row_node in row_nodes:
        row_cells = table.add_row().cells
        col_cursor = 0
        for cell_node in row_node.get("content", []):
            if col_cursor >= num_cols:
                break
            colspan = int(cell_node.get("attrs", {}).get("colspan", 1))
            colspan = max(1, min(colspan, num_cols - col_cursor))
            target_cell = row_cells[col_cursor]
            target_cell.text = _extract_text(cell_node)
            if colspan > 1:
                target_cell.merge(row_cells[col_cursor + colspan - 1])
            col_cursor += colspan


def _add_image(doc: DocxDocument, node: dict) -> None:
    """لا تُضمَّن الصورة الحقيقية في ملف Word أبداً (طلب صريح: المترجم يعمل على النص
    فقط عبر Trados/MateCat، وإدراج الصورة الفعلية مكانها مهمة منفصلة لاحقة لفريق DTP) —
    بدلاً من ذلك تُكتَب عبارة Placeholder نصية واضحة، بنفس صيغة الـplaceholders التلقائية
    القادمة أصلاً من `ingest._insert_image_placeholders` (`[Insert Image_XX here]`)، كي
    يتّسق الشكل بصرف النظر عن كون الصورة أُدرِجَت تلقائياً أو بالسحب اليدوي من مكتبة
    الوسائط. الصورة الفعلية تُسلَّم بدلاً من ذلك في مجلد `images/` ضمن ملف ZIP التصدير."""
    image_id = node.get("attrs", {}).get("imageId")
    doc.add_paragraph(f"[Insert {image_id or 'Image'} here]")


@router.post("/export-docx")
def export_docx(payload: ExportRequest) -> StreamingResponse:
    top_content = payload.content.get("content", [])
    if not top_content:
        raise HTTPException(status_code=422, detail="المستند فارغ، لا يوجد محتوى للتصدير")

    doc = DocxDocument()
    for node in top_content:
        node_type = node.get("type")
        try:
            if node_type in ("paragraph", "heading"):
                _add_paragraph_or_heading(doc, node)
            elif node_type == "table":
                _add_table(doc, node)
            elif node_type == "image":
                _add_image(doc, node)
        except Exception as exc:  # عنصر واحد فاشل لا يوقف تصدير بقية المستند
            logger.warning("تعذّر تصدير عنصر من نوع %s: %s", node_type, exc)

    docx_buffer = io.BytesIO()
    doc.save(docx_buffer)
    docx_buffer.seek(0)

    safe_name = _safe_file_name(payload.file_name)

    if not payload.images:
        return StreamingResponse(
            docx_buffer,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.docx"'},
        )

    # مستند فيه صور: يُسلَّم ZIP واحد (Word + مجلد images/) بدل ملف .docx مفرد، كي يجد
    # المترجم/فريق DTP الصور التي تشير إليها الـplaceholders النصية داخل Word بسهولة.
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr(f"{safe_name}.docx", docx_buffer.getvalue())
        for image in payload.images:
            extension = image.mime_type.rsplit("/", maxsplit=1)[-1] or "png"
            zip_file.writestr(f"images/{image.image_id}.{extension}", base64.b64decode(image.data_base64))
    zip_buffer.seek(0)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )


def _node_to_html(node: dict) -> str:
    node_type = node.get("type")
    if node_type == "text":
        return html.escape(node.get("text", ""))
    if node_type == "paragraph":
        inner = "".join(_node_to_html(child) for child in node.get("content", []))
        return f"<p>{inner}</p>" if inner else "<p>&nbsp;</p>"
    if node_type == "heading":
        level = min(max(int(node.get("attrs", {}).get("level") or 1), 1), 6)
        inner = "".join(_node_to_html(child) for child in node.get("content", []))
        return f"<h{level}>{inner}</h{level}>"
    if node_type == "table":
        rows_html = []
        for row in node.get("content", []):
            cells_html = []
            for cell in row.get("content", []):
                tag = "th" if cell.get("type") == "tableHeader" else "td"
                colspan = int(cell.get("attrs", {}).get("colspan", 1))
                colspan_attr = f' colspan="{colspan}"' if colspan > 1 else ""
                inner = "".join(_node_to_html(child) for child in cell.get("content", []))
                cells_html.append(f"<{tag}{colspan_attr}>{inner}</{tag}>")
            rows_html.append(f"<tr>{''.join(cells_html)}</tr>")
        return f"<table>{''.join(rows_html)}</table>"
    if node_type == "image":
        src = node.get("attrs", {}).get("src", "")
        return f'<img src="{html.escape(src)}" />' if src else ""
    return ""


def _content_to_html_document(content: dict) -> str:
    body = "".join(_node_to_html(node) for node in content.get("content", []))
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
  body {{ font-family: -apple-system, "Segoe UI", Tahoma, Arial, sans-serif;
          padding: 20px; line-height: 1.6; color: #111; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  td, th {{ border: 1px solid #333; padding: 6px 10px; text-align: start; }}
  th {{ background: #f2f2f2; }}
  img {{ max-width: 100%; margin: 12px 0; display: block; }}
  p:empty::before {{ content: "\\00a0"; }}
</style>
</head>
<body>{body}</body>
</html>"""


@router.post("/export-pdf")
def export_pdf(payload: ExportRequest) -> StreamingResponse:
    top_content = payload.content.get("content", [])
    if not top_content:
        raise HTTPException(status_code=422, detail="المستند فارغ، لا يوجد محتوى للتصدير")

    html_document = _content_to_html_document(payload.content)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(html_document, wait_until="load")
                pdf_bytes = page.pdf(
                    format="A4",
                    print_background=True,
                    margin={"top": "20mm", "bottom": "20mm", "left": "15mm", "right": "15mm"},
                )
            finally:
                browser.close()
    except Exception as exc:
        logger.error("فشل توليد PDF عبر Playwright: %s", exc)
        raise HTTPException(status_code=500, detail=f"فشل توليد PDF: {exc}") from exc

    safe_name = _safe_file_name(payload.file_name)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.pdf"'},
    )

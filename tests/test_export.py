"""اختبار ميزة استخراج الأصول (Assets): تصدير Word بلا صور يُعيد .docx مفرداً كما
كان، بينما وجود صور يُعيد ZIP واحداً (Word + مجلد images/) بدل تضمين الصورة الحقيقية
داخل Word — راجع `medical_ocr/api/routers/export.py`."""

import io
import unittest
import zipfile

from docx import Document as DocxDocument
from fastapi.testclient import TestClient

from medical_ocr.api.app import app


class TestExportDocx(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_export_without_images_returns_plain_docx(self):
        content = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
            ],
        }

        response = self.client.post(
            "/export-docx", json={"content": content, "file_name": "no_images", "images": []}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        docx_doc = DocxDocument(io.BytesIO(response.content))
        self.assertEqual(docx_doc.paragraphs[0].text, "Hello")

    def test_export_with_images_returns_zip_with_placeholder_and_png_folder(self):
        content = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Before"}]},
                {"type": "image", "attrs": {"src": "data:image/png;base64,ignored", "imageId": "Image_01"}},
                {"type": "paragraph", "content": [{"type": "text", "text": "After"}]},
            ],
        }
        images = [{"image_id": "Image_01", "mime_type": "image/png", "data_base64": "aGVsbG8="}]

        response = self.client.post(
            "/export-docx", json={"content": content, "file_name": "with_images", "images": images}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")

        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            names = zf.namelist()
            self.assertIn("with_images.docx", names)
            self.assertIn("images/Image_01.png", names)

            with zf.open("with_images.docx") as f:
                docx_doc = DocxDocument(io.BytesIO(f.read()))
                paragraph_texts = [p.text for p in docx_doc.paragraphs]
                self.assertEqual(paragraph_texts, ["Before", "[Insert Image_01 here]", "After"])
                # لا صورة حقيقية مُضمَّنة في المستند — فقرات نصية فقط، بلا أي "InlineShape"
                self.assertEqual(len(docx_doc.inline_shapes), 0)

            with zf.open("images/Image_01.png") as f:
                self.assertEqual(f.read(), b"hello")

    def test_arabic_file_name_does_not_crash_response_headers(self):
        # اسم ملف عربي (شائع جداً لملفات المستخدم الفعلية) كان يمر دون تغيير عبر
        # `_safe_file_name` (لأن `\w` في Python يطابق يونيكود أيضاً)، فيُسبِّب
        # UnicodeEncodeError عند وضعه في ترويسة Content-Disposition (يجب أن تكون
        # latin-1) — اختُبِر هذا الخطأ فعلياً عبر الواجهة الحية قبل إصلاحه.
        content = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hi"}]}]}

        response = self.client.post(
            "/export-docx",
            json={"content": content, "file_name": "تقرير_DEXA_للمريضة", "images": []},
        )

        self.assertEqual(response.status_code, 200)
        response.headers["content-disposition"]  # لا يرفع استثناءً عند الوصول إليها


if __name__ == "__main__":
    unittest.main()

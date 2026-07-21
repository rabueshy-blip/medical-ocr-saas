import json
import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import Mock, patch

import dspy
import fitz
import requests
from PIL import Image

import medical_ocr.ingest as ingest_module
from medical_ocr.ingest import (
    VisionAPIError,
    _blocks_from_vision_page,
    _call_vision_api,
    _compress_image_to_limit,
    _get_vision_api_key,
    extract_document,
)
from medical_ocr.schema import BlockType, PageSource, SourceEngine


def _make_pdf(path: str, text: str = None) -> None:
    doc = fitz.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def _make_table_pdf(path: str, rows) -> None:
    """يبني PDF بجدول حقيقي بخطوط شبكة صريحة — pdfplumber.find_tables() يحتاج خطوطاً
    فعلية على الصفحة لاكتشاف الجدول، وليس مجرد نص مُحاذى بمسافات."""
    doc = fitz.open()
    page = doc.new_page()
    x0, y0 = 50, 50
    col_w, row_h = 150, 30
    num_cols = len(rows[0])

    for r in range(len(rows) + 1):
        y = y0 + r * row_h
        page.draw_line((x0, y), (x0 + num_cols * col_w, y))
    for c in range(num_cols + 1):
        x = x0 + c * col_w
        page.draw_line((x, y0), (x, y0 + len(rows) * row_h))

    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((x0 + c * col_w + 5, y0 + r * row_h + 20), cell)

    doc.save(path)
    doc.close()


def _make_tight_borderless_table_pdf(path: str, rows, col_x) -> None:
    """يبني PDF بجدول بلا خطوط شبكة (نص مُحاذى بالمواضع فقط، كنتائج مخبرية شائعة) مع
    أعمدة متقاربة جداً عمداً — يستخدم لاختبار أن التفاوت الديناميكي
    (`_dynamic_text_table_settings`) يمنع التحام نص عمودين متجاورين في خلية واحدة."""
    doc = fitz.open()
    page = doc.new_page()
    row_h = 25
    for r, row in enumerate(rows):
        y = 50 + r * row_h
        for c, cell in enumerate(row):
            page.insert_text((col_x[c], y), cell, fontsize=10)
    doc.save(path)
    doc.close()


class TestExtractDocument(unittest.TestCase):
    def test_digital_page_uses_pymupdf_text_without_ocr(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "digital.pdf")
            _make_pdf(pdf_path, text="Patient report: blood pressure 140/90 mmHg")

            document = extract_document(pdf_path, file_name="digital.pdf")

            self.assertEqual(len(document.pages), 1)
            page = document.pages[0]
            self.assertEqual(page.source, PageSource.DIGITAL)
            paragraph_blocks = [b for b in page.blocks if b.block_type == BlockType.PARAGRAPH]
            self.assertTrue(paragraph_blocks)
            self.assertTrue(all(b.source_engine == SourceEngine.PYMUPDF for b in paragraph_blocks))
            joined_text = " ".join(b.text for b in paragraph_blocks)
            self.assertIn("blood pressure", joined_text)

    def test_digital_page_extracts_real_grid_table_via_pdfplumber(self):
        rows = [["Drug", "Dose"], ["Metformin", "500mg"], ["Aspirin", "100mg"]]
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "table.pdf")
            _make_table_pdf(pdf_path, rows)

            document = extract_document(pdf_path, file_name="table.pdf")

            table_blocks = [b for b in document.pages[0].blocks if b.block_type == BlockType.TABLE]
            self.assertEqual(len(table_blocks), 1)
            self.assertEqual(table_blocks[0].rows, rows)
            self.assertEqual(table_blocks[0].source_engine, SourceEngine.PDFPLUMBER)

    def test_borderless_table_with_tight_columns_does_not_bleed_into_neighbor_cell(self):
        # عمود "Result" ("95"، "4.2") وعمود "Unit" ("mg/dL"، "mmol/L") متباعدان عمداً
        # ~3px فقط (أقل بكثير من التفاوت الثابت القديم 5px) — قبل التفاوت الديناميكي كان
        # هذا يُنتج "ResultUnit"/"95 mg/dL" ملتحمَين في خلية واحدة (تحقّقنا من هذا فعلياً
        # عبر تشغيل الإعداد الثابت القديم مباشرة قبل كتابة هذا الاختبار).
        rows = [
            ["Test", "Result", "Unit", "Range"],
            ["Glucose", "95", "mg/dL", "70-99"],
            ["Potassium", "4.2", "mmol/L", "3.5-5.0"],
        ]
        col_x = [50, 150, 181, 260]
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "tight.pdf")
            _make_tight_borderless_table_pdf(pdf_path, rows, col_x)

            document = extract_document(pdf_path, file_name="tight.pdf")

            table_blocks = [b for b in document.pages[0].blocks if b.block_type == BlockType.TABLE]
            self.assertEqual(len(table_blocks), 1)
            self.assertEqual(table_blocks[0].rows, rows)

    def test_embedded_image_is_extracted_as_png_with_placeholder_in_correct_position(self):
        # ميزة استخراج الأصول: صورة مُضمَّنة بين فقرتين يجب أن (1) تُستخرَج كـImageAsset
        # مستقل بصيغة PNG، و(2) يظهر Placeholder نصي واضح في مكانها الأصلي بين الفقرتين
        # في تدفّق المستند (وليس مُلحَقاً في آخر الصفحة).
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "with_image.pdf")
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((50, 50), "Before the chart")
            image = Image.new("RGB", (60, 40), (0, 128, 255))
            buf = BytesIO()
            image.save(buf, format="JPEG")
            page.insert_image(fitz.Rect(50, 100, 150, 180), stream=buf.getvalue())
            page.insert_text((50, 220), "After the chart")
            doc.save(pdf_path)
            doc.close()

            document = extract_document(pdf_path, file_name="with_image.pdf")

            self.assertEqual(len(document.images), 1)
            asset = document.images[0]
            self.assertEqual(asset.mime_type, "image/png")
            self.assertEqual(asset.image_id, "Image_01")
            self.assertIsNotNone(asset.bbox)

            texts = [b.text for b in document.pages[0].blocks if b.text]
            self.assertEqual(
                texts, ["Before the chart", "[Insert Image_01 here]", "After the chart"]
            )

    @patch("medical_ocr.ingest._scanned_page_blocks_vision", return_value=[])
    def test_blank_page_is_routed_to_scanned_ocr_path(self, mock_scanned_blocks):
        # لا نستدعي Google Vision API الحقيقي هنا — فقط نتحقق أن صفحة بلا طبقة نص
        # تُوجَّه لمسار OCR الممسوح، دون أي استدعاء شبكة حقيقي.
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "blank.pdf")
            _make_pdf(pdf_path, text=None)

            document = extract_document(pdf_path, file_name="blank.pdf")

            self.assertEqual(document.pages[0].source, PageSource.SCANNED)
            mock_scanned_blocks.assert_called_once()

    @patch(
        "medical_ocr.ingest._scanned_page_blocks_vision",
        side_effect=VisionAPIError("500 من الخادم بعد 3 محاولات"),
    )
    def test_scanned_page_vision_failure_does_not_crash_whole_document(self, _mock):
        # فشل صفحة ممسوحة واحدة (بعد استنفاد إعادة المحاولة) يجب ألا يوقف استخراج
        # بقية المستند — يُسجَّل بدلاً منه Block واحد واضح الفشل بدل استثناء غير مُلتقَط.
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "blank.pdf")
            _make_pdf(pdf_path, text=None)

            document = extract_document(pdf_path, file_name="blank.pdf")

            page = document.pages[0]
            self.assertEqual(page.source, PageSource.SCANNED)
            self.assertEqual(len(page.blocks), 1)
            self.assertEqual(page.blocks[0].confidence, 0.0)
            self.assertIn("تعذّر", page.blocks[0].text)
            self.assertEqual(page.blocks[0].source_engine, SourceEngine.GOOGLE_VISION)


def _random_image_bytes(width: int, height: int, fmt: str = "PNG") -> bytes:
    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


class TestCompressImageToLimit(unittest.TestCase):
    def test_returns_original_bytes_unchanged_when_already_within_limit(self):
        small = _random_image_bytes(20, 20)

        result = _compress_image_to_limit(small, max_bytes=len(small) + 1000)

        self.assertIs(result, small)

    def test_compresses_via_jpeg_quality_reduction_when_over_limit(self):
        # صورة PNG لضجيج عشوائي 1200x1200 (~4.3MB) — أسوأ حالة ضغط ممكنة، تحاكي صفحة
        # ممسوحة بدقة DPI عالية تتجاوز حد Vision API.
        large = _random_image_bytes(1200, 1200)
        max_bytes = 600_000

        result = _compress_image_to_limit(large, max_bytes=max_bytes)

        self.assertLessEqual(len(result), max_bytes)
        result_image = Image.open(BytesIO(result))
        self.assertEqual(result_image.format, "JPEG")
        # الجودة وحدها كفت هنا — لا حاجة لتصغير الأبعاد.
        self.assertEqual(result_image.size, (1200, 1200))

    def test_shrinks_dimensions_when_quality_reduction_alone_is_not_enough(self):
        large = _random_image_bytes(1200, 1200)
        max_bytes = 200_000

        result = _compress_image_to_limit(large, max_bytes=max_bytes)

        self.assertLessEqual(len(result), max_bytes)
        result_image = Image.open(BytesIO(result))
        self.assertLess(result_image.width, 1200)
        self.assertLess(result_image.height, 1200)

    def test_raises_when_limit_is_impossibly_small(self):
        large = _random_image_bytes(1200, 1200)

        with self.assertRaises(VisionAPIError):
            _compress_image_to_limit(large, max_bytes=50)


class TestGetVisionApiKey(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_missing_key_raises_clear_error(self):
        with self.assertRaises(VisionAPIError) as ctx:
            _get_vision_api_key()
        self.assertIn("GOOGLE_VISION_API_KEY", str(ctx.exception))

    @patch.dict(os.environ, {"GOOGLE_VISION_API_KEY": "test-key"}, clear=True)
    def test_present_key_is_returned(self):
        self.assertEqual(_get_vision_api_key(), "test-key")


def _fake_response(status_code: int, json_body: dict = None, text: str = "") -> Mock:
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_body or {}
    response.text = text
    return response


class TestCallVisionApi(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"GOOGLE_VISION_API_KEY": "test-key"}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep_patcher = patch("medical_ocr.ingest.time.sleep", return_value=None)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    @patch("medical_ocr.ingest.requests.post")
    def test_retries_on_connection_error_then_succeeds(self, mock_post):
        success = _fake_response(200, {"responses": [{"fullTextAnnotation": {"pages": []}}]})
        mock_post.side_effect = [
            requests.exceptions.ConnectionError("انقطاع مؤقت"),
            requests.exceptions.Timeout("مهلة"),
            success,
        ]

        result = _call_vision_api(b"fake-image-bytes")

        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(result, {"fullTextAnnotation": {"pages": []}})

    @patch("medical_ocr.ingest.requests.post")
    def test_retries_on_server_error_then_succeeds(self, mock_post):
        success = _fake_response(200, {"responses": [{"fullTextAnnotation": {"pages": []}}]})
        mock_post.side_effect = [_fake_response(500, text="internal error"), success]

        result = _call_vision_api(b"fake-image-bytes")

        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(result, {"fullTextAnnotation": {"pages": []}})

    @patch("medical_ocr.ingest.requests.post")
    def test_does_not_retry_on_client_error(self, mock_post):
        mock_post.return_value = _fake_response(400, text="API key invalid")

        with self.assertRaises(VisionAPIError):
            _call_vision_api(b"fake-image-bytes")

        # لا فائدة من إعادة محاولة مفتاح غير صالح — يجب الفشل من أول محاولة فقط.
        self.assertEqual(mock_post.call_count, 1)

    @patch("medical_ocr.ingest.requests.post")
    def test_raises_after_exhausting_retries_on_repeated_server_error(self, mock_post):
        mock_post.return_value = _fake_response(500, text="internal error")

        with self.assertRaises(VisionAPIError):
            _call_vision_api(b"fake-image-bytes")

        self.assertEqual(mock_post.call_count, ingest_module._VISION_MAX_ATTEMPTS)

    @patch("medical_ocr.ingest.requests.post")
    def test_vision_api_error_field_in_200_response_raises(self, mock_post):
        mock_post.return_value = _fake_response(
            200, {"responses": [{"error": {"message": "Bad image data"}}]}
        )

        with self.assertRaises(VisionAPIError) as ctx:
            _call_vision_api(b"fake-image-bytes")
        self.assertIn("Bad image data", str(ctx.exception))
        self.assertEqual(mock_post.call_count, 1)


class TestBlocksFromVisionPage(unittest.TestCase):
    def test_parses_paragraphs_into_paragraph_blocks_with_averaged_confidence(self):
        vision_page = {
            "blocks": [
                {
                    "paragraphs": [
                        {
                            "boundingBox": {
                                "vertices": [
                                    {"x": 10, "y": 20},
                                    {"x": 110, "y": 20},
                                    {"x": 110, "y": 40},
                                    {"x": 10, "y": 40},
                                ]
                            },
                            "words": [
                                {
                                    "confidence": 0.9,
                                    "symbols": [{"text": "B"}, {"text": "P"}],
                                },
                                {
                                    "confidence": 0.7,
                                    "symbols": [{"text": "1"}, {"text": "4"}, {"text": "0"}],
                                },
                            ],
                        }
                    ]
                }
            ]
        }

        blocks = _blocks_from_vision_page(vision_page)

        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block.block_type, BlockType.PARAGRAPH)
        self.assertEqual(block.text, "BP 140")
        self.assertEqual(block.source_engine, SourceEngine.GOOGLE_VISION)
        self.assertAlmostEqual(block.confidence, 0.8)
        self.assertEqual(block.bbox.x0, 10)
        self.assertEqual(block.bbox.y1, 40)

    def test_empty_paragraph_text_is_skipped(self):
        vision_page = {"blocks": [{"paragraphs": [{"words": []}]}]}
        self.assertEqual(_blocks_from_vision_page(vision_page), [])


def _vision_word(text, x0, x1, top, bottom):
    """يبني dict كلمة بنفس شكل مخرَج `_vision_word_boxes` مباشرة (للاختبارات التي
    تفحص التجميع الهندسي دون المرور عبر JSON استجابة Vision كاملة)."""
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": bottom}


def _dexa_style_words(include_surrounding_paragraph: bool) -> list:
    """يبني كلمات جدول DEXA اصطناعي (Region/BMD/T-Score/Z-Score) بفجوات أفقية واسعة
    بين الأعمدة، مع خيار تضمين سطر نص عادي محيط (لاختبار عدم التأثر بنسبة الجدول من
    الصفحة — انظر توثيق `_detect_scanned_table_regions`).

    كل الفجوات هنا (نثر ~20-30px، أعمدة ~165-270px) مبنية على قياسات حقيقية فعلية
    عبر Vision API حياً على صفحات DEXA حقيقية (وليست أرقاماً مُخترَعة) — نسخة أولى
    من هذا الملف استخدمت فجوات أعمدة صغيرة جداً (~60-85px) قريبة جداً من فجوات
    النثر ففشلت لاحقاً في التمييز، ونسخة أخرى استخدمت فجوة "Femoral"→"Neck" شبه
    صفرية (3px) غير واقعية أصلاً."""
    words = []
    if include_surrounding_paragraph:
        for text, x0, x1 in [
            ("Patient:", 50, 110), ("Jane", 128, 158), ("Doe,", 176, 206),
            ("DOB", 224, 254), ("1985-03-12", 272, 352),
        ]:
            words.append(_vision_word(text, x0, x1, 50, 65))
    for text, x0, x1 in [("Region", 100, 160), ("BMD", 400, 435), ("T-Score", 600, 660), ("Z-Score", 900, 960)]:
        words.append(_vision_word(text, x0, x1, 120, 135))
    for text, x0, x1 in [("L1-L4", 100, 150), ("0.912", 410, 445), ("-1.2", 610, 640), ("-0.5", 910, 935)]:
        words.append(_vision_word(text, x0, x1, 150, 165))
    for text, x0, x1 in [
        ("Femoral", 90, 140), ("Neck", 158, 183), ("0.850", 410, 445), ("-1.8", 610, 640), ("-1.1", 910, 935),
    ]:
        words.append(_vision_word(text, x0, x1, 180, 195))
    return words


class TestVisionWordBoxes(unittest.TestCase):
    def test_extracts_word_text_and_pixel_bbox(self):
        vision_page = {
            "blocks": [
                {
                    "paragraphs": [
                        {
                            "words": [
                                {
                                    "symbols": [{"text": "B"}, {"text": "P"}],
                                    "boundingBox": {
                                        "vertices": [
                                            {"x": 10, "y": 20}, {"x": 30, "y": 20},
                                            {"x": 30, "y": 40}, {"x": 10, "y": 40},
                                        ]
                                    },
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        words = ingest_module._vision_word_boxes(vision_page)

        self.assertEqual(len(words), 1)
        self.assertEqual(words[0]["text"], "BP")
        self.assertEqual((words[0]["x0"], words[0]["top"], words[0]["x1"], words[0]["bottom"]), (10, 20, 30, 40))

    def test_word_without_bounding_box_is_skipped(self):
        vision_page = {"blocks": [{"paragraphs": [{"words": [{"symbols": [{"text": "X"}]}]}]}]}
        self.assertEqual(ingest_module._vision_word_boxes(vision_page), [])


class TestDetectScannedTableRegions(unittest.TestCase):
    """اكتشاف جداول الصفحات الممسوحة هندسياً (بلا أي كيان "جدول" من Vision نفسه) —
    انظر توثيق `_find_gap_threshold`/`_detect_scanned_table_regions` لتاريخ التطوير
    (وسيط ← أضيق فجوة ← أكبر قفزة نسبية ← أول قفزة نسبية بدءاً من الأسفل، كل خطوة
    كانت رداً على فشل حقيقي مُلاحَظ ضد Vision API فعلي وليس افتراضياً)."""

    def test_detects_dexa_table_alongside_surrounding_paragraph(self):
        regions = ingest_module._detect_scanned_table_regions(_dexa_style_words(include_surrounding_paragraph=True))

        self.assertEqual(len(regions), 1)
        self.assertEqual(
            regions[0]["rows"],
            [
                ["Region", "BMD", "T-Score", "Z-Score"],
                ["L1-L4", "0.912", "-1.2", "-0.5"],
                ["Femoral Neck", "0.850", "-1.8", "-1.1"],
            ],
        )

    def test_detects_dexa_table_when_page_is_almost_entirely_tabular(self):
        regions = ingest_module._detect_scanned_table_regions(_dexa_style_words(include_surrounding_paragraph=False))

        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]["rows"][0], ["Region", "BMD", "T-Score", "Z-Score"])

    def test_two_unrelated_tables_separated_by_whitespace_are_not_merged(self):
        # اختبار مبني على خلل حقيقي اكتُشف عبر ملف مريض حقيقي: جدول DEXA رئيسي
        # يتبعه مباشرة (بلا سطر نثر يفصل بينهما في قائمة الأسطر) جدول مرجعي مختلف
        # تماماً (معايير WHO)، بمسافة رأسية بيضاء واضحة أكبر من التباعد المعتاد بين
        # أسطر الصفحة — يجب أن يُكتشَفا كمنطقتين منفصلتين، لا منطقة واحدة ملتحمة.
        words = _dexa_style_words(include_surrounding_paragraph=False)
        normal_line_spacing = 30  # يطابق التباعد بين أسطر الجدول الأول (top=120/150/180)
        second_table_top = 180 + 15 + normal_line_spacing * 6  # فجوة أكبر بكثير من المعتاد
        # 3 أسطر (وليس 2) عمداً كي يبقى هذا الجدول فوق `_SCANNED_TABLE_MIN_ROWS` الحالي
        # (رُفع لـ3 لاستبعاد أزواج حقول الترويسة الوهمية — انظر توثيق الثابت).
        for i, (text, x0, x1) in enumerate([("Normal", 100, 160), ("Elevated", 100, 170), ("High", 100, 150)]):
            words.append(_vision_word(text, x0, x1, second_table_top + i * normal_line_spacing, second_table_top + i * normal_line_spacing + 15))
        for i, (text, x0, x1) in enumerate([("Range", 400, 460), ("Range", 400, 460), ("Range", 400, 460)]):
            words.append(_vision_word(text, x0, x1, second_table_top + i * normal_line_spacing, second_table_top + i * normal_line_spacing + 15))

        regions = ingest_module._detect_scanned_table_regions(words)

        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0]["rows"][0], ["Region", "BMD", "T-Score", "Z-Score"])
        self.assertEqual(regions[1]["rows"], [["Normal", "Range"], ["Elevated", "Range"], ["High", "Range"]])

    def test_plain_prose_page_yields_no_false_positive_table(self):
        words = []
        y = 50
        for line in [
            "This is a normal paragraph sentence with several words in it.",
            "Here is another line of plain prose text following the first one.",
            "And a third line to make sure no false table is detected here.",
        ]:
            x = 50
            for token in line.split(" "):
                width = len(token) * 6
                words.append(_vision_word(token, x, x + width, y, y + 15))
                x += width + 5
            y += 25

        self.assertEqual(ingest_module._detect_scanned_table_regions(words), [])

    def test_single_row_is_not_enough_to_count_as_a_table(self):
        words = [
            _vision_word("Region", 100, 160, 120, 135),
            _vision_word("BMD", 220, 250, 120, 135),
        ]
        self.assertEqual(ingest_module._detect_scanned_table_regions(words), [])


class TestStructureScannedTableRows(unittest.TestCase):
    def test_returns_none_when_lm_not_configured(self):
        # بيئة الاختبار بلا GEMINI_API_KEY أصلاً (نفس افتراض بقية هذا الملف)، لذا
        # dspy.settings.lm يبقى None طوال تشغيل المجموعة كاملة.
        self.assertIsNone(dspy.settings.lm)
        self.assertIsNone(ingest_module._structure_scanned_table_rows([["Region", "BMD"], ["L1-L4", "0.912"]]))

    def test_parses_structured_dicts_into_flat_corrected_rows_when_lm_configured(self):
        raw_rows = [["Regoin", "BMD"], ["L1-L4", "0.912"]]
        fake_structurer = Mock(
            return_value=Mock(
                structured_rows=json.dumps(
                    [{"Region": "Region", "BMD": "BMD"}, {"Region": "L1-L4", "BMD": "0.912"}]
                )
            )
        )
        original_lm = dspy.settings.lm
        dspy.settings.lm = "fake-lm-for-test"
        try:
            with patch.object(ingest_module, "_get_table_structurer", return_value=fake_structurer):
                result = ingest_module._structure_scanned_table_rows(raw_rows)
        finally:
            dspy.settings.lm = original_lm

        self.assertEqual(result, [["Region", "BMD"], ["L1-L4", "0.912"]])

    def test_falls_back_to_none_when_row_count_mismatches(self):
        raw_rows = [["Region", "BMD"], ["L1-L4", "0.912"]]
        fake_structurer = Mock(return_value=Mock(structured_rows=json.dumps([{"Region": "Region", "BMD": "BMD"}])))
        original_lm = dspy.settings.lm
        dspy.settings.lm = "fake-lm-for-test"
        try:
            with patch.object(ingest_module, "_get_table_structurer", return_value=fake_structurer):
                result = ingest_module._structure_scanned_table_rows(raw_rows)
        finally:
            dspy.settings.lm = original_lm

        self.assertIsNone(result)

    def test_falls_back_to_none_when_lm_call_raises(self):
        fake_structurer = Mock(side_effect=RuntimeError("network error"))
        original_lm = dspy.settings.lm
        dspy.settings.lm = "fake-lm-for-test"
        try:
            with patch.object(ingest_module, "_get_table_structurer", return_value=fake_structurer):
                result = ingest_module._structure_scanned_table_rows([["Region"], ["L1-L4"]])
        finally:
            dspy.settings.lm = original_lm

        self.assertIsNone(result)


class TestMergeSplitHeaderRow(unittest.TestCase):
    """خلل حقيقي وُصِف من المستخدم بعد رفع ملف DEXA حقيقي ("بعض الأرقام تظهر
    وبعضها لا"): ترويسة مطبوعة على سطرين ("Site Region BMD Young Adult Age
    Matched" ثم "(gm/cm2) T-score Z-score" تحتهما مباشرة) كانت تُبنى كصفّين
    منفصلين، فيملأ LLM خلايا Site/Region الناقصة في السطر الثاني بـ"UNCERTAIN" —
    `_merge_split_header_row` يدمجهما قبل وصولهما لـLLM أصلاً."""

    def test_merges_shorter_second_row_right_aligned_into_first(self):
        rows = [
            ["Site", "Region", "BMD", "Young Adult", "Age Matched"],
            ["( gm / cm2 )", "T - score", "Z - score"],
            ["Spine", "Total", "0.846", "-2.1", "-1.2"],
        ]

        merged = ingest_module._merge_split_header_row(rows)

        self.assertEqual(
            merged,
            [
                ["Site", "Region", "BMD ( gm / cm2 )", "Young Adult T - score", "Age Matched Z - score"],
                ["Spine", "Total", "0.846", "-2.1", "-1.2"],
            ],
        )

    def test_leaves_rows_unchanged_when_second_row_is_not_shorter(self):
        rows = [["Test", "Result"], ["Glucose", "95"], ["Sodium", "140"]]
        self.assertEqual(ingest_module._merge_split_header_row(rows), rows)

    def test_leaves_single_row_unchanged(self):
        self.assertEqual(ingest_module._merge_split_header_row([["Only"]]), [["Only"]])


class TestLooksLikeLabelValueMetadata(unittest.TestCase):
    """خلل حقيقي: رفع `_SCANNED_TABLE_MIN_ROWS` لاستبعاد أزواج NAME/FILE-NO الوهمية
    استبعد أيضاً جدولاً سريرياً حقيقياً قصيراً (صفّان فقط) — الفارق الفعلي محتوى
    الخلايا (كل خلية "تسمية: قيمة" بذاتها) وليس عدد الصفوف."""

    def test_two_rows_of_colon_pairs_is_metadata(self):
        rows = [["NAME : MARZOOKA SLYM", "FILE NO : 252036"], ["DATE : 14/10/2025", "REF : Dr. SHAIMA"]]
        self.assertTrue(ingest_module._looks_like_label_value_metadata(rows))

    def test_two_rows_of_plain_numeric_data_is_not_metadata(self):
        rows = [["Neck", "0.698", "-1.9", "-1.2"], ["Total", "0.757", "-1.5", "-1.1"]]
        self.assertFalse(ingest_module._looks_like_label_value_metadata(rows))

    def test_three_row_table_is_never_metadata_regardless_of_content(self):
        rows = [["A : 1", "B : 2"], ["C : 3", "D : 4"], ["E : 5", "F : 6"]]
        self.assertFalse(ingest_module._looks_like_label_value_metadata(rows))

    def test_mixed_row_with_one_plain_cell_is_not_metadata(self):
        rows = [["NAME : MARZOOKA SLYM", "252036"], ["DATE : 14/10/2025", "SHAIMA"]]
        self.assertFalse(ingest_module._looks_like_label_value_metadata(rows))


class TestScannedTableBlocks(unittest.TestCase):
    def test_builds_table_block_with_raw_grid_when_lm_unconfigured(self):
        blocks = ingest_module._scanned_table_blocks(_dexa_style_words(include_surrounding_paragraph=True))

        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block.block_type, BlockType.TABLE)
        self.assertEqual(block.source_engine, SourceEngine.GOOGLE_VISION)
        self.assertEqual(block.rows, block.raw_rows)
        self.assertEqual(block.rows[0], ["Region", "BMD", "T-Score", "Z-Score"])


class TestScannedPageTableIntegration(unittest.TestCase):
    """اختبار تكامل كامل: `_scanned_page_blocks_vision` يجب أن يبني Block(TABLE) واحداً
    من الجدول ويستبعد نصه من الفقرات العادية (بلا تكرار)، مع بقاء ترتيب القراءة صحيحاً
    (الفقرة قبل الجدول)."""

    @patch("medical_ocr.ingest._call_vision_api")
    def test_table_extracted_once_without_duplicating_paragraph_text(self, mock_call_vision_api):
        words = _dexa_style_words(include_surrounding_paragraph=True)

        def make_word(w):
            return {
                "symbols": [{"text": ch} for ch in w["text"]],
                "boundingBox": {
                    "vertices": [
                        {"x": w["x0"], "y": w["top"]}, {"x": w["x1"], "y": w["top"]},
                        {"x": w["x1"], "y": w["bottom"]}, {"x": w["x0"], "y": w["bottom"]},
                    ]
                },
                "confidence": 0.95,
            }

        # سطر الفقرة العادية بمفرده كـparagraph منفصل عن كلمات الجدول، بنفس التقسيم
        # الحقيقي الذي يعطيه Vision (فقرة لكل سطر هنا لتبسيط الاختبار).
        para_words = [w for w in words if w["text"] in ("Patient:", "Jane", "Doe,", "DOB", "1985-03-12")]
        table_words = [w for w in words if w not in para_words]

        def paragraph_from(ws):
            xs0 = min(w["x0"] for w in ws)
            xs1 = max(w["x1"] for w in ws)
            ys0 = min(w["top"] for w in ws)
            ys1 = max(w["bottom"] for w in ws)
            return {
                "boundingBox": {
                    "vertices": [
                        {"x": xs0, "y": ys0}, {"x": xs1, "y": ys0}, {"x": xs1, "y": ys1}, {"x": xs0, "y": ys1},
                    ]
                },
                "words": [make_word(w) for w in ws],
            }

        # كل سطر جدول فقرة Vision منفصلة (واقعي: Vision يقسّم كل سطر لفقرة عادة).
        table_lines = {}
        for w in table_words:
            table_lines.setdefault(w["top"], []).append(w)

        vision_page = {
            "blocks": [
                {
                    "paragraphs": [paragraph_from(para_words)]
                    + [paragraph_from(line_words) for line_words in table_lines.values()]
                }
            ]
        }
        mock_call_vision_api.return_value = {"fullTextAnnotation": {"pages": [vision_page]}}

        # صفحة fitz حقيقية (فارغة) فقط لتوليد raster حقيقي يمرّ عبر
        # `_compress_image_to_limit` بدون أخطاء — استدعاء Vision API نفسه مموَّه أعلاه.
        real_doc = fitz.open()
        real_page = real_doc.new_page()
        blocks = ingest_module._scanned_page_blocks_vision(real_page)
        real_doc.close()

        table_blocks = [b for b in blocks if b.block_type == BlockType.TABLE]
        paragraph_blocks = [b for b in blocks if b.block_type == BlockType.PARAGRAPH]

        self.assertEqual(len(table_blocks), 1)
        self.assertEqual(table_blocks[0].rows[0], ["Region", "BMD", "T-Score", "Z-Score"])

        # نص الجدول (مثال "BMD") يجب ألا يظهر مكرَّراً كفقرة منفصلة أيضاً.
        paragraph_texts = " ".join(b.text for b in paragraph_blocks)
        self.assertIn("Patient", paragraph_texts)
        self.assertNotIn("T-Score", paragraph_texts)

        # ترتيب القراءة: الفقرة (أعلى الصفحة) يجب أن تسبق الجدول في قائمة blocks.
        self.assertLess(blocks.index(paragraph_blocks[0]), blocks.index(table_blocks[0]))


if __name__ == "__main__":
    unittest.main()

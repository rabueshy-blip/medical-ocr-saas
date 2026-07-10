# خطة بناء أداة Micro-SaaS لاستخراج OCR طبي هجين (Hybrid Medical OCR)

## 1. الهدف

بناء نظام يستخرج النصوص والجداول من ملفات PDF (نص رقمي أو صفحات ممسوحة ضوئياً) بدقة عالية جداً، مع منع الهلوسة تماماً — موجه للمترجمين الطبيين والأطباء.

## 2. المبدأ الأساسي لمنع الهلوسة

لا يُسمح لأي نموذج لغوي/رؤية (LLM) بتوليد نص من الصفر. كل نموذج يعمل كـ**مُصحِّح** لنص استخرجه محرك OCR كلاسيكي أولاً (Ground Truth Anchor)، وليس كمولّد حر. كل تصحيح يُحفظ مع النص الأصلي لغرض التتبع (audit trail).

## 3. البنية الهندسية (Architecture)

### المرحلة 0 — التصنيف لكل صفحة (Triage)
- فتح الصفحة عبر `PyMuPDF` والتحقق من وجود طبقة نص حقيقية.
- توجيه كل صفحة إلى المسار المناسب (بعض الملفات مختلطة: صفحات رقمية + صفحات ممسوحة).

### المسار أ — PDF رقمي (Digital)
- `PyMuPDF` (fitz): استخراج النص + الإحداثيات (bbox) + الخطوط.
- `pdfplumber` / `Camelot`: استخراج الجداول.
- لا تدخّل لأي LLM هنا (النص موثوق 99%+، أي "تصحيح" هنا خطر هلوسة غير ضروري).

### المسار ب — صفحات ممسوحة ضوئياً (Scanned)
1. **طبقة 1 — Ground Truth:** `PaddleOCR` (+ وحدة `PP-Structure` لاكتشاف الجداول وتفكيكها إلى خلايا). يعطي نص + bbox + درجة ثقة (confidence) لكل كلمة.
2. **طبقة 2 — تحقق مزدوج:** تشغيل `Tesseract` كمحرك ثانٍ، ومقارنة (diff) النتيجتين. أي كلمة مختلَف عليها تُعلَّم كمنطقة "منخفضة الاتفاق".
3. **طبقة 3 — Vision LLM كمصحح مقيّد:** تمرير صورة المنطقة المشكوك فيها فقط (crop، وليس الصفحة كاملة) + النص المستخرج إلى نموذج Vision (Claude / GPT-4V) بتعليمات صارمة: تصحيح فقط بناءً على الصورة، عدم إضافة كلمات غير موجودة، وإرجاع `UNCERTAIN` عند عدم اليقين.
4. **الجداول الممسوحة:** اكتشاف بنية الجدول (صفوف/أعمدة) عبر `PP-Structure` أو `Table Transformer (TATR)` أولاً كإحداثيات، ثم تمرير كل خلية للـ OCR بشكل منفصل بدل الجدول ككتلة واحدة.

## 4. مخطط البيانات الموحّد (Unified Schema)

```
Document
 └─ Page (رقم، مصدر: digital/scanned)
     └─ Block (نوع: paragraph / table / heading)
         ├─ text أو rows[][] (للجداول)
         ├─ bbox
         ├─ confidence
         └─ source_engine (pymupdf / paddleocr / llm_corrected)
```

`confidence` و `source_engine` ضروريان لاحقاً لإعطاء أولوية للمراجعة البشرية في المناطق منخفضة الثقة.

## 5. التقطيع (Chunking) قبل DSPy

- التقطيع حسب الوحدة الدلالية (Block)، وليس حسب عدد رموز ثابت.
- كل جدول = chunk واحد كامل (لا يُفصل الرأس عن الصفوف).
- كل فقرة/عنوان = chunk، مع نافذة تداخل (overlap) صغيرة بين الفقرات المتتالية.
- إرفاق بيانات وصفية (رقم الصفحة، confidence، source_engine) كسياق يُمرَّر لموديول DSPy، وليس كنص يُصحَّح.

## 6. تكامل DSPy

- Signature منفصلة لكل مهمة: تصحيح إملاء طبي، ضبط أعمدة/صفوف الجدول، إعادة صياغة — بدون دمجها في موديول واحد.
- ترسيخ (Grounding) تصحيح الإملاء الطبي بمرجع خارجي (قاموس مصطلحات طبية / قائمة أدوية وتشخيصات محلية) عبر بحث تشابه (fuzzy matching أو embeddings) كخطوة Retrieve قبل موديول DSPy.
- استخدام `dspy.Assert` / `dspy.Suggest` كقيود صارمة (مثال: الكلمة المصححة يجب أن تطابق قاموس المصطلحات أو نمط جرعة/رقم معروف) كخط دفاع برمجي، وليس مجرد تعليمة نصية.
- الاحتفاظ بالنص الأصلي (raw) بجانب المُصحَّح لكل تصحيح، لأغراض التتبع (audit trail).

## 7. الاعتماديات المتوقعة (Dependencies)

> **قيد بيئي:** الحساب الحالي على الماك ليس حساب Administrator ولا توجد صلاحيات sudo، لذا لا يمكن تثبيت Homebrew أو أي اعتمادية نظام (system binary مثل `tesseract-ocr` أو `ghostscript`). تم تعديل قائمة الاعتماديات لتعتمد **حصراً على مكتبات Python المُثبَّتة عبر pip** داخل بيئة افتراضية (venv):

- `pymupdf` (fitz) — استخراج نص/جداول PDF الرقمي.
- `pdfplumber` — استخراج نص وجداول PDF الرقمي (بديل بايثون خالص لـ Camelot، لا يحتاج ghostscript). **تم إسقاط `camelot-py`** لأنه يعتمد على ghostscript كاعتمادية نظام غير متاحة.
- `paddleocr` + `paddlepaddle` (يشمل PP-Structure) — محرك OCR الأساسي (الطبقة 1) للصفحات الممسوحة.
- `easyocr` — محرك OCR ثانٍ للتحقق المزدوج (الطبقة 2)، **بديل عن `pytesseract`/`tesseract-ocr`** لأن الأخير يتطلب تثبيت ثنائي نظام غير متاح بدون Homebrew.
- `rapidfuzz` — مقارنة/diff بين نتائج المحركين، وربط تصحيح الإملاء الطبي بالقاموس المرجعي (خطوة Retrieve).
- `dspy-ai`
- `anthropic` — عميل API لنموذج Vision (Claude) كمصحح مقيّد.
- `streamlit` — واجهة المستخدم.
- `opencv-python-headless`, `numpy`, `pandas`, `Pillow` — اعتماديات مساعدة لمحركات OCR ومعالجة الصور.
- قاموس/مرجع مصطلحات طبية (يُحدَّد لاحقاً حسب اللغة المستهدفة)

## 8. خطة التنفيذ المرحلية (خطوات لاحقة، بدون كتابة كود بعد)

1. إعداد بيئة المشروع والاعتماديات.
2. بناء وحدة التصنيف (Triage) لكل صفحة.
3. بناء مسار PDF الرقمي (PyMuPDF + pdfplumber/Camelot).
4. بناء مسار الصفحات الممسوحة (PaddleOCR + Tesseract + مقارنة/diff).
5. دمج طبقة تصحيح Vision LLM المقيّدة بالمناطق منخفضة الثقة فقط.
6. توحيد المخرجات ضمن الـ Schema الموحد.
7. بناء وحدة التقطيع (Chunking) حسب الوحدة الدلالية.
8. تصميم DSPy Signatures والموديولات (تصحيح إملائي، ضبط جداول، إعادة صياغة) مع قيود Assert/Suggest.
9. اختبار شامل على عينات PDF حقيقية (رقمية + ممسوحة + مختلطة) وقياس الدقة يدوياً.

## 9. الحالة

تم اعتماد هذه الخطة.

- **الخطوة 1 (إعداد البيئة والاعتماديات): مكتملة ✅**
  - تم إنشاء بيئة افتراضية `.venv` (Python 3.9.6، النظامي على macOS arm64).
  - تم تثبيت جميع الاعتماديات (`requirements.txt`) عبر pip فقط: pymupdf, pdfplumber, paddlepaddle, paddleocr, easyocr, rapidfuzz, dspy-ai, anthropic, streamlit, opencv-python-headless, numpy, pandas, Pillow.
  - تم التحقق عبر اختبار استيراد (smoke test) لجميع المكتبات الحرجة، بما فيها paddleocr و easyocr و torch (مع دعم تسريع MPS على Apple Silicon) و dspy — جميعها تعمل دون أخطاء.
  - **لم يتم** تثبيت Homebrew/tesseract-ocr/ghostscript بسبب غياب صلاحيات الإدارة على الحساب؛ تم استبدالها ببدائل بايثون خالصة (انظر قسم 7).
- الخطوات 2-5 (Triage، مسار PDF الرقمي، مسار الصفحات الممسوحة، طبقة Vision LLM) لم تبدأ بعد.
- **الخطوات 6 و7 (Schema الموحّد + Chunking) وجزء من الخطوة 8 (أول Signature-ين لـ DSPy): مكتملة ✅** (اليوم الثالث، المرحلة 2)
  - `medical_ocr/schema.py`: نماذج Pydantic لـ `Document`/`Page`/`Block` مطابقة للمخطط الموحّد في القسم 4 (نوع الـ block، bbox، confidence، source_engine)، مع حفظ `raw_text`/`raw_rows` تلقائياً كنسخة تدقيق (audit trail) قبل أي تصحيح لاحق. تتضمن محوّلات (adapters) من المخرجات الخام الفعلية لـ `pymupdf` (span dict)، `pdfplumber` (extract_table)، و`paddleocr` (نتيجة سطر واحد).
  - `medical_ocr/chunking.py`: `chunk_document()` يقطّع حسب الوحدة الدلالية (Block) وليس عدد رموز ثابت — الجدول = chunk كامل غير مجزّأ، والفقرات تحمل نافذة تداخل (`context_before`) كحقل ميتاداتا منفصل لا يُدمج داخل النص المراد تصحيحه.
  - `medical_ocr/terminology.py`: `MedicalTerminologyRetriever` — خطوة Retrieve عبر `rapidfuzz` فوق قاموس محلي (`data/medical_terms_sample.txt`، **عيّنة مؤقتة placeholder** يجب استبدالها بمرجع رسمي حسب اللغة المستهدفة لاحقاً، انظر القسم 7).
  - `medical_ocr/signatures/spelling.py`: أول `dspy.Signature` (`MedicalSpellingCorrection`) + موديول `MedicalSpellingCorrector` يستدعي خطوة Retrieve قبل الموديل، ويطبّق `dspy.Suggest` كقيد ترسيخ برمجي فعلي (`is_correction_grounded`: يرفض إعادة صياغة كاملة أو إضافة كلمات كثيرة) بدل الاكتفاء بتعليمة نصية.
  - `medical_ocr/signatures/tables.py`: ثاني `dspy.Signature` (`MedicalTableStructuring`) + موديول `MedicalTableStructurer` يفرض `dspy.Suggest` على الحفاظ على عدد الصفوف (`row_count_preserved`) لمنع دمج/حذف صفوف الجدول.
  - اختبارات دخان (`tests/`، عبر `unittest` القياسية بدون pytest) تغطي schema/chunking/terminology وبنية الـ Signatures والقيود البرمجية الصرفة، دون أي استدعاء LM حقيقي (29 اختباراً، كلها ناجحة).
  - **لم يبدأ بعد (حتى نهاية اليوم الثالث):** دمج `dspy.Assert` (وليس `Suggest` فقط)، تكوين LM فعلي عبر `anthropic`/`litellm`، بناء موديول "إعادة الصياغة" الثالث المذكور في القسم 6، وربط كل هذا بمخرجات حقيقية من خطوات Triage/الاستخراج (لأنها لم تُبنَ بعد).
- باقي بنود الخطوة 8 وكذلك الخطوة 9 (الاختبار الشامل على عينات PDF حقيقية) لم تبدأ بعد.

## 11. اليوم الخامس — ضبط الجودة (QA): بناء Gold Dataset وتقييم كمّي ومُحسِّن DSPy

- **Gold Dataset (مكتمل بنيوياً ✅):** 15 عيّنة طبية "صعبة" منسّقة (10 حالات مصطلحات في `data/gold/terminology.json` + 5 حالات جداول في `data/gold/tables.json`)، كل عيّنة تحمل مخرجات مرجعية (`expected_correction_terms`, `forbidden_terms`, `expected_uncertain_terms` للمصطلحات؛ `expected_row_values`, `expected_uncertain_row_indices` للجداول) — بخلاف `medical_ocr/eval_cases.py` (اليوم الرابع) الذي يحمل raw+note للمراجعة اليدوية فقط دون مخرجات مرجعية قابلة للقياس البرمجي.
  - الحالات مصمَّمة عمداً لتغطي: التباس أسماء أدوية بالسياق السريري، خلط حرف/رقم (OCR شائع في النصوص المكتوبة بخط اليد)، كلمات غير مقروءة يجب أن تُعامَل كـ`UNCERTAIN` بدل اختراعها، أسماء تجارية غير موجودة بالقاموس المرجعي (اختبار ضبط النفس عن الاستبدال التلقائي)، أرقام جرعة واضحة لكن غير معتادة سريرياً (اختبار عدم "تصحيح" رقم واضح بحجة أنه غير مألوف)، رؤوس جداول مقسومة على أسطر بخلايا مدمجة، انزياح أعمدة، صفوف متشابهة ظاهرياً يجب ألا تُدمَج، وجداول بلا `column_hints` إطلاقاً.
  - `medical_ocr/gold_dataset.py`: نماذج Pydantic (`GoldTerminologyExample`, `GoldTableExample`, `GoldDataset`) + `load_gold_dataset()`، بالإضافة إلى `build_terminology_devset()`/`build_table_devset()` اللتين تحوّلان كل عيّنة إلى `dspy.Example` (مع `with_inputs` محدَّدة بدقة) لإعادة استخدامهما من كل من سكربت التقييم والمُحسِّن دون ازدواج منطق التحويل.
- **سكربت التقييم الكمّي (مكتمل بنيوياً ✅، بانتظار مفتاح API):** `scripts/evaluate_gold.py` يشغّل الموديولين عبر `dspy.Evaluate` ضد Gold Dataset كاملاً، ويطبع/يحفظ درجة إجمالية لكل مجموعة بالإضافة إلى تفصيل لكل عيّنة (raw/prediction/score) في `scripts/gold_eval_results.json` — أول قياس كمّي فعلي للدقة في المشروع، بخلاف اليوم الرابع الذي اكتفى بمراجعة يدوية لحقل reasoning.
- **دوال القياس (مكتمل ✅):** `medical_ocr/gold_metrics.py` (`terminology_metric`, `table_metric`) — مشتركة بين سكربتي التقييم والتحسين لتفادي الازدواج. تطبّق نفس مبدأ منع الهلوسة من القسم 2 كبوابة صارمة أولاً (`is_correction_grounded`/`row_count_preserved` يُفشلان الدرجة كاملة إلى 0.0)، ثم درجة جزئية عبر fuzzy matching (`rapidfuzz.fuzz.partial_ratio`) لمطابقة المصطلحات/الخلايا المتوقعة، وليس مطابقة نصية حرفية.
- **مُحسِّن DSPy (Optimizer) (مكتمل بنيوياً ✅، بانتظار مفتاح API):** `scripts/optimize_modules.py` يستخدم `dspy.teleprompt.BootstrapFewShot` (وليس `MIPROv2`، غير مناسب لحجم بيانات 15 عيّنة فقط) لتوليد demos تلقائياً من قسم train في كل مجموعة (7/3 للمصطلحات، 3/2 للجداول — تقسيم train/dev صارم لتفادي تسريب البيانات)، مع `metric_threshold=0.8` على نفس دوال `gold_metrics`. يقيس درجة dev قبل/بعد التحسين للمقارنة، ويحفظ حالة الموديولين المُحسَّنين (demos فقط، JSON عبر `Module.save(save_program=False)`) تحت `scripts/optimized/`.
  - تحقّق بنيوي (بدون LM): `named_predictors()` على كلا الموديولين يكتشف الـ predictor الداخلي بشكل صحيح رغم التغليف بـ `dspy.Refine` (`correct.module.predict` / `structure.module.predict`)، و`reset_copy()`/`deepcopy()` يعملان دون أخطاء — شرط أساسي لعمل `BootstrapFewShot.compile()`.
- اختبارات دخان جديدة بدون LM حقيقي (`tests/test_gold_dataset.py`, `tests/test_gold_metrics.py`) — المجموع الآن 50 اختباراً، كلها ناجحة.
- **التالي:** بمجرد توفر مفتاح Anthropic API، تشغيل `scripts/evaluate_gold.py` (خط أساس قبل أي تحسين) ثم `scripts/optimize_modules.py`، ومراجعة الفرق قبل/بعد يدوياً. لاحقاً: توسعة القاموس المرجعي (لا يزال placeholder، القسم 7) والنظر في زيادة حجم Gold Dataset إن أظهر التقييم الأول ثغرات غير مغطاة.

## 10. اليوم الرابع — دمج موديولات التفكير وتطوير دقة الاستخراج العالية

- **تكوين LM فعلي (جزئياً مكتمل ✅، بانتظار مفتاح API):**
  - `medical_ocr/lm_config.py`: `configure_lm()` يحمّل `.env` (عبر `python-dotenv`، أُضيف إلى `requirements.txt`) ويُعدّ `dspy.settings.configure` بنموذج `dspy.LM("anthropic/claude-sonnet-5")` (قابل للتغيير عبر `MEDICAL_OCR_LM_MODEL`). يرفع `RuntimeError` برسالة عربية واضحة إن غاب `ANTHROPIC_API_KEY` بدل خطأ شبكة غامض من LiteLLM.
  - **لم يُنفَّذ بعد فعلياً ضد LM حقيقي** — لا يوجد مفتاح Anthropic API متاح في هذه البيئة حتى الآن. بمجرد توفره، التشغيل عبر `scripts/run_hard_cases.py`.
- **حالات اختبار "صعبة" لقياس دقة CoT (مكتمل ✅):** `medical_ocr/eval_cases.py` يحوي حالتين للمصطلحات (`drug_name_ambiguity`: كلمة OCR مشوَّهة تتطابق تشابهياً مع دوائين مختلفين "ميتفورمين"/"ميتوبرولول" ويجب حسمها بالسياق السريري لا بالتشابه الحرفي فقط؛ `ocr_digit_letter_confusion`: خلط حرف O بدل الرقم 0 في جرعة دواء) وحالة جدول واحدة (`multi_level_lab_header`: رأس جدول مقسوم على سطرين بسبب خلايا مدمجة + خلية قيمة مفقودة يجب أن تصبح `UNCERTAIN`). أُضيف "ميتوبرولول" إلى `data/medical_terms_sample.txt` لجعل حالة التباس اسم الدواء حقيقية (تحقّق تلقائي في `tests/test_eval_cases.py` أن الكلمة الملتبسة تُطابق فعلاً المصطلحين معاً عبر `retrieve_candidate_terms`).
- **`scripts/run_hard_cases.py` (مكتمل بنيوياً ✅، بانتظار تشغيل فعلي):** يُشغّل `MedicalSpellingCorrector`/`MedicalTableStructurer` على الحالات الصعبة، ويطبع/يحفظ حقل `reasoning` (تفكير CoT الفعلي المُتاح من `dspy.ChainOfThought`، تم التحقق أنه موجود ضمن مخرجات كلا الموديولين) إلى جانب المخرجات، كأول تشغيل حقيقي (وليس بنيوي فقط) لموديولات DSPy في المشروع.
- اختبارات دخان جديدة بدون LM حقيقي (`tests/test_lm_config.py`, `tests/test_eval_cases.py`) — المجموع الآن 33 اختباراً، كلها ناجحة.
- **هيكل FastAPI (مكتمل ✅):** `medical_ocr/api/` (`app.py`, `dependencies.py`, `lm_guard.py`, `schemas.py`, `routers/spelling.py`, `routers/tables.py`) يُغلّف `MedicalSpellingCorrector`/`MedicalTableStructurer` بنقاط API. `lm_guard.py` يمنع الإقلاع من الفشل الصامت إن غاب مفتاح Anthropic API (يرجع 503 بدل خطأ غامض — مغطى باختبارات `tests/test_api.py` بدون LM حقيقي). تم تشغيل السيرفر عبر uvicorn والتحقق يدوياً ثم إغلاقه.
- **التالي:** بمجرد توفر مفتاح Anthropic API، تشغيل `scripts/run_hard_cases.py` فعلياً ومراجعة جودة التفكير/الدقة يدوياً ضد LM حقيقي — لم يحدث بعد.

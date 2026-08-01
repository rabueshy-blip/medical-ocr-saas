import axios from "axios";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

// يُرسَل مع كل طلب عبر رأس X-API-Key (medical_ocr/api/auth.py على الباك-إند). ملاحظة
// مهمة: أي متغيّر NEXT_PUBLIC_* يُضمَّن فعلياً داخل حزمة JS المُرسَلة للمتصفح، فهو مرئي
// لأي شخص يفحص طلبات الشبكة — هذا يمنع بوتات عشوائية تفحص الإنترنت بحثاً عن نقاط API
// مفتوحة، لكنه **لا يمنع** منافساً مصمّماً يفحص أدوات المطوّر. حماية أقوى تتطلب حساب
// مستخدم حقيقي (تسجيل دخول) بدل مفتاح مشترك واحد.
const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: process.env.NEXT_PUBLIC_API_KEY
    ? { "X-API-Key": process.env.NEXT_PUBLIC_API_KEY }
    : {},
});

export type BlockType = "paragraph" | "heading" | "table";

export interface BoundingBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface Block {
  block_type: BlockType;
  text: string | null;
  raw_text: string | null;
  rows: string[][] | null;
  raw_rows: string[][] | null;
  colspans: number[][] | null;
  bbox: BoundingBox | null;
  confidence: number;
  source_engine: string;
  category: string | null;
}

export interface Page {
  page_number: number;
  source: "digital" | "scanned";
  blocks: Block[];
}

export interface ImageAsset {
  page_number: number;
  index: number;
  image_id: string;
  mime_type: string;
  data_base64: string;
  width: number;
  height: number;
  bbox: BoundingBox | null;
}

export interface Document {
  file_name: string;
  pages: Page[];
  images: ImageAsset[];
}

export function imageAssetSrc(image: ImageAsset): string {
  return `data:${image.mime_type};base64,${image.data_base64}`;
}

export async function extractDocument(file: File): Promise<Document> {
  const formData = new FormData();
  formData.append("file", file);

  const response = await apiClient.post<Document>(
    "/extract-document",
    formData,
    { headers: { "Content-Type": "multipart/form-data" } },
  );
  return response.data;
}

/** استخراج ملف PowerPoint حديث (.pptx فقط) — انظر `medical_ocr/ingest_pptx.py`
 * لسبب استبعاد .ppt القديم عمداً. لا streaming هنا (خلاف `extractDocumentStream`)
 * لأن الاستخراج بنيوي مباشر (لا OCR/LM)، سريع بما يكفي لطلب HTTP عادي واحد. */
export async function extractPptx(file: File): Promise<Document> {
  const formData = new FormData();
  formData.append("file", file);

  const response = await apiClient.post<Document>(
    "/extract-pptx",
    formData,
    { headers: { "Content-Type": "multipart/form-data" } },
  );
  return response.data;
}

/** نسخة Server-Sent Events من `extractDocument` — نفس النتيجة النهائية بالضبط، لكن
 * تستدعي `onProgress` بعد كل صفحة منجزة أثناء الانتظار. مستند 30 صفحة ممسوحة (حد
 * الخطة المجانية) يستغرق فعلياً ~2.5 دقيقة (استدعاء Vision API حقيقي متسلسل لكل
 * صفحة) — بلا هذا التقدّم، الواجهة تبقى شاشة تحميل صامتة تبدو "عالقة" طوال المدة.
 * axios لا يدعم قراءة SSE قطعة-بقطعة بسهولة هنا، فنستخدم `fetch` مباشرة.
 *
 * **بثّ حقيقي للبيانات (تصحيح ذاكرة على الخادم):** كانت هذه الدالة تنتظر حدث "done"
 * الوحيد الذي يحمل المستند كاملاً — يعني الخادم كان يُبقي كل صفحات/صور المستند
 * مُجمَّعة في ذاكرته طوال الطلب رغم البثّ الظاهري (رُصِد فعلياً أن ملفاً ~17MB كثيف
 * الصور يُسقط خادم Render بتجاوز حد الذاكرة 512MB). الآن كل صفحة تصل عبر حدث "page"
 * منفصل فور اكتمالها في الخادم، فيُسقطها الخادم من ذاكرته فوراً بعد إرسالها بدل
 * الاحتفاظ بها حتى النهاية — التجميع النهائي في مستند واحد يحدث هنا في المتصفح بدلاً
 * من الخادم، لأن ذاكرة المتصفح ليست القيد الفعلي (خلاف حاوية Render الصغيرة). */
export async function extractDocumentStream(
  file: File,
  onProgress: (page: number, total: number) => void,
): Promise<Document> {
  const formData = new FormData();
  formData.append("file", file);

  const headers: Record<string, string> = {};
  if (process.env.NEXT_PUBLIC_API_KEY) {
    headers["X-API-Key"] = process.env.NEXT_PUBLIC_API_KEY;
  }

  const response = await fetch(`${API_BASE_URL}/extract-document-stream`, {
    method: "POST",
    headers,
    body: formData,
  });

  if (!response.ok || !response.body) {
    throw new Error(`تعذّر الاتصال بخادم الاستخراج (HTTP ${response.status})`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const pages: Page[] = [];
  const images: ImageAsset[] = [];

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // كل حدث SSE مفصول بسطر فارغ (\n\n)؛ آخر جزء غير مكتمل يبقى في buffer لحين
    // اكتمال القطعة التالية من الشبكة.
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";

    for (const part of parts) {
      const line = part.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      const payload = JSON.parse(line.slice("data: ".length));

      if (payload.type === "progress") {
        onProgress(payload.page, payload.total);
      } else if (payload.type === "page") {
        pages.push(payload.page as Page);
        images.push(...(payload.images as ImageAsset[]));
      } else if (payload.type === "done") {
        return { file_name: file.name, pages, images };
      } else if (payload.type === "error") {
        throw new Error(payload.message || "تعذّر استخراج المستند");
      }
    }
  }

  throw new Error("انقطع الاتصال بخادم الاستخراج قبل اكتمال العملية");
}

export type ExportFormat = "docx" | "pptx" | "pdf";

export interface ExportImage {
  image_id: string;
  mime_type: string;
  data_base64: string;
}

/** عند تصدير Word مع وجود صور، يُرجع الخادم ملف ZIP واحد (Word + مجلد images/) بدل
 * .docx مفرد — انظر `export_docx` في `medical_ocr/api/routers/export.py`. */
export async function exportFile(
  format: ExportFormat,
  content: unknown,
  fileName: string,
  images: ExportImage[] = [],
): Promise<Blob> {
  const response = await apiClient.post(
    `/export-${format}`,
    { content, file_name: fileName, images },
    { responseType: "blob" },
  );
  return response.data;
}

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

export type ExportFormat = "docx" | "pdf";

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

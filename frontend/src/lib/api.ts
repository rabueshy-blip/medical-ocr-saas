import axios from "axios";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

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
  mime_type: string;
  data_base64: string;
  width: number;
  height: number;
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

  const response = await axios.post<Document>(
    `${API_BASE_URL}/extract-document`,
    formData,
    { headers: { "Content-Type": "multipart/form-data" } },
  );
  return response.data;
}

export type ExportFormat = "docx" | "pdf";

export async function exportFile(
  format: ExportFormat,
  content: unknown,
  fileName: string,
): Promise<Blob> {
  const response = await axios.post(
    `${API_BASE_URL}/export-${format}`,
    { content, file_name: fileName },
    { responseType: "blob" },
  );
  return response.data;
}

import type { Block, Document, ImageAsset } from "@/lib/api";
import { imageAssetSrc } from "@/lib/api";

// يطابق نص الـplaceholder التلقائي الذي يكتبه `_insert_image_placeholders` في
// ingest.py حرفياً ("[Insert Image_01 here]") كي يُستبدَل بعقدة صورة TipTap حقيقية
// (نفس النوع الناتج عن السحب اليدوي من مكتبة الوسائط في EditorPane.tsx) بدل نص خام.
const IMAGE_PLACEHOLDER_PATTERN = /^\[Insert (\S+) here\]$/;

/** يحوّل Document القادم من /extract-document إلى محتوى JSON يفهمه محرر TipTap،
 * محافظاً على رقم الصفحة وbbox لكل فقرة/جدول (انظر tiptapBlockExtensions.ts). */
export function documentToTiptapContent(document: Document) {
  const imagesById = new Map<string, ImageAsset>(
    document.images.map((image) => [image.image_id, image]),
  );
  const content: Record<string, unknown>[] = [];

  for (const page of document.pages) {
    for (const block of page.blocks) {
      const node = blockToNode(block, page.page_number, imagesById);
      if (node) content.push(node);
    }
  }

  return {
    type: "doc",
    content: content.length > 0 ? content : [{ type: "paragraph" }],
  };
}

function blockToNode(
  block: Block,
  pageNumber: number,
  imagesById: Map<string, ImageAsset>,
): Record<string, unknown> | null {
  const locationAttrs = {
    page: pageNumber,
    bbox: block.bbox ? JSON.stringify(block.bbox) : null,
  };

  const placeholderMatch = block.block_type === "paragraph" && block.text
    ? block.text.match(IMAGE_PLACEHOLDER_PATTERN)
    : null;
  if (placeholderMatch) {
    const image = imagesById.get(placeholderMatch[1]);
    if (image) {
      // LocatableImage لا يعرّف page/bbox في مخطّطه (خلاف الفقرة/العنوان/الجدول) —
      // فقط src/imageId، نفس ما يُنتجه السحب اليدوي من مكتبة الوسائط (EditorPane.tsx).
      return {
        type: "image",
        attrs: { src: imageAssetSrc(image), imageId: image.image_id },
      };
    }
    // لا صورة مطابقة (حالة غير متوقعة) — يبقى كنص عادي بدل فقدان الفقرة بصمت.
  }

  if (block.block_type === "table" && block.rows) {
    return {
      type: "table",
      attrs: locationAttrs,
      content: block.rows.map((row, rowIndex) => ({
        type: "tableRow",
        content: row.map((cellText, cellIndex) => {
          const colspan = block.colspans?.[rowIndex]?.[cellIndex] ?? 1;
          return {
            type: rowIndex === 0 ? "tableHeader" : "tableCell",
            // colspan افتراضي 1 من إضافة الجدول نفسها في TipTap — لا يُضاف attrs.colspan
            // إلا عند دمج فعلي (>1) مكتشف من PDF المصدر، للحفاظ على JSON نظيف.
            ...(colspan > 1 ? { attrs: { colspan } } : {}),
            content: [
              {
                type: "paragraph",
                content: cellText ? [{ type: "text", text: cellText }] : [],
              },
            ],
          };
        }),
      })),
    };
  }

  const text = block.text ?? "";
  return {
    type: block.block_type === "heading" ? "heading" : "paragraph",
    attrs:
      block.block_type === "heading"
        ? { ...locationAttrs, level: 2 }
        : locationAttrs,
    content: text ? [{ type: "text", text }] : [],
  };
}

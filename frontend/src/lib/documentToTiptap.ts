import type { Block, Document } from "@/lib/api";

/** يحوّل Document القادم من /extract-document إلى محتوى JSON يفهمه محرر TipTap،
 * محافظاً على رقم الصفحة وbbox لكل فقرة/جدول (انظر tiptapBlockExtensions.ts). */
export function documentToTiptapContent(document: Document) {
  const content: Record<string, unknown>[] = [];

  for (const page of document.pages) {
    for (const block of page.blocks) {
      const node = blockToNode(block, page.page_number);
      if (node) content.push(node);
    }
  }

  return {
    type: "doc",
    content: content.length > 0 ? content : [{ type: "paragraph" }],
  };
}

function blockToNode(block: Block, pageNumber: number): Record<string, unknown> | null {
  const locationAttrs = {
    page: pageNumber,
    bbox: block.bbox ? JSON.stringify(block.bbox) : null,
  };

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

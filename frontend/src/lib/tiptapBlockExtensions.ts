import Paragraph from "@tiptap/extension-paragraph";
import Heading from "@tiptap/extension-heading";
import { Table } from "@tiptap/extension-table";

/**
 * كل عقدة فقرة/عنوان/جدول تحمل موقعها الأصلي في الـPDF (رقم الصفحة + bbox)
 * كـ data-attributes، لتمكين ميزة "اضغط على النص → تحديد مكانه في PDF"
 * (EditorPane.tsx يقرأها عبر event.target.closest('[data-page]')).
 */
function locationAttributes() {
  return {
    page: {
      default: null as number | null,
      parseHTML: (element: HTMLElement) => {
        const value = element.getAttribute("data-page");
        return value ? Number(value) : null;
      },
      renderHTML: (attributes: Record<string, unknown>) => {
        if (!attributes.page) return {};
        return { "data-page": attributes.page };
      },
    },
    bbox: {
      default: null as string | null,
      parseHTML: (element: HTMLElement) => element.getAttribute("data-bbox"),
      renderHTML: (attributes: Record<string, unknown>) => {
        if (!attributes.bbox) return {};
        return { "data-bbox": attributes.bbox };
      },
    },
  };
}

export const LocatableParagraph = Paragraph.extend({
  addAttributes() {
    return { ...this.parent?.(), ...locationAttributes() };
  },
});

export const LocatableHeading = Heading.extend({
  addAttributes() {
    return { ...this.parent?.(), ...locationAttributes() };
  },
});

export const LocatableTable = Table.extend({
  addAttributes() {
    return { ...this.parent?.(), ...locationAttributes() };
  },
});

"use client";

import { useState } from "react";
import type { Editor } from "@tiptap/react";
import { exportFile, type ExportFormat } from "@/lib/api";
import { useDocumentStore } from "@/store/useDocumentStore";

export function ExportButton({ editor }: { editor: Editor | null }) {
  const document = useDocumentStore((state) => state.document);
  const [exportingFormat, setExportingFormat] = useState<ExportFormat | null>(null);

  async function handleExport(format: ExportFormat) {
    if (!editor) return;
    setExportingFormat(format);
    try {
      const baseName = (document?.file_name ?? "translated_document").replace(/\.pdf$/i, "");
      const blob = await exportFile(format, editor.getJSON(), baseName);

      const url = URL.createObjectURL(blob);
      const link = window.document.createElement("a");
      link.href = url;
      link.download = `${baseName}.${format}`;
      link.click();
      URL.revokeObjectURL(url);
    } finally {
      setExportingFormat(null);
    }
  }

  const disabled = !editor || exportingFormat !== null;

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        onClick={() => handleExport("docx")}
        disabled={disabled}
        className="rounded-md bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-white dark:text-zinc-900"
      >
        {exportingFormat === "docx" ? "جارٍ التصدير..." : "تصدير Word"}
      </button>
      <button
        type="button"
        onClick={() => handleExport("pdf")}
        disabled={disabled}
        className="rounded-md border border-zinc-300 px-3 py-1.5 text-xs font-medium text-zinc-900 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-white dark:hover:bg-zinc-800"
      >
        {exportingFormat === "pdf" ? "جارٍ التصدير..." : "تصدير PDF"}
      </button>
    </div>
  );
}

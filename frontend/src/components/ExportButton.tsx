"use client";

import { useState } from "react";
import type { Editor } from "@tiptap/react";
import { isAxiosError } from "axios";
import { exportFile, type ExportFormat } from "@/lib/api";
import { useDocumentStore } from "@/store/useDocumentStore";

/** يستخرج رسالة خطأ مقروءة من فشل axios عند `responseType: "blob"` — في هذه الحالة
 * `error.response.data` نفسه Blob (حتى لو الخادم أرجع JSON فعلياً)، فلا يكفي قراءته
 * مباشرة كنص. بلا هذا، أي فشل تصدير (401/429/500/انقطاع اتصال) كان يُسقَط بصمت في
 * `finally` فقط، فيبدو للمستخدم أن الزر "لا يعمل" رغم أن الطلب فشل فعلياً. */
async function extractErrorMessage(err: unknown): Promise<string> {
  if (isAxiosError(err)) {
    if (err.response?.data instanceof Blob) {
      try {
        const text = await err.response.data.text();
        const parsed = JSON.parse(text);
        if (typeof parsed.detail === "string") return parsed.detail;
      } catch {
        // ليس JSON قابلاً للتحليل — نتابع للرسالة العامة أدناه.
      }
    }
    if (err.response?.status === 429) {
      return "عدد كبير جداً من الطلبات خلال وقت قصير — حاول مرة أخرى بعد دقيقة.";
    }
    if (err.response?.status) {
      return `تعذّر التصدير (HTTP ${err.response.status})`;
    }
    return "تعذّر الاتصال بخادم التصدير — تحقّق من اتصالك بالإنترنت وحاول مرة أخرى.";
  }
  return err instanceof Error ? err.message : "تعذّر تصدير الملف";
}

const Spinner = () => (
  <svg className="h-3 w-3 animate-spin" viewBox="0 0 24 24" fill="none">
    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
  </svg>
);

export function ExportButton({ editor }: { editor: Editor | null }) {
  const document = useDocumentStore((state) => state.document);
  const [exportingFormat, setExportingFormat] = useState<ExportFormat | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  // يُعرض بعد نجاح التنزيل فعلياً (وليس فقط انتهاء "Exporting...") لأن الملف ينزل بصمت
  // تامة لمجلد Downloads بلا أي نافذة حفظ أو تنبيه من المتصفح نفسه — بدون هذا التأكيد
  // الصريح، نجاح حقيقي وفشل صامت يبدوان متطابقين تماماً للمستخدم (نفس السبب الجذري
  // لبلاغات "الزر لا يعمل" رغم أن الملفات كانت تنزل فعلاً).
  const [justDownloaded, setJustDownloaded] = useState<ExportFormat | null>(null);

  async function handleExport(format: ExportFormat) {
    if (!editor) return;
    setExportingFormat(format);
    setErrorMessage(null);
    setJustDownloaded(null);
    try {
      const baseName = (document?.file_name ?? "translated_document").replace(/\.pdf$/i, "");
      const images = format === "docx" || format === "pptx" ? (document?.images ?? []) : [];
      const blob = await exportFile(format, editor.getJSON(), baseName, images);

      // مستند Word/PowerPoint فيه صور يعود من الخادم كملف ZIP (الملف + مجلد images/)
      // بدل ملف مفرد — الامتداد يُحدَّد من نوع الاستجابة الفعلي، وليس من `format` وحده.
      const extension = blob.type === "application/zip" ? "zip" : format;

      const url = URL.createObjectURL(blob);
      const link = window.document.createElement("a");
      link.href = url;
      link.download = `${baseName}.${extension}`;
      link.click();
      URL.revokeObjectURL(url);

      setJustDownloaded(format);
      setTimeout(() => setJustDownloaded(null), 4000);
    } catch (err) {
      setErrorMessage(await extractErrorMessage(err));
    } finally {
      setExportingFormat(null);
    }
  }

  const disabled = !editor || exportingFormat !== null;

  function label(format: ExportFormat, idle: string) {
    if (exportingFormat === format) {
      return (
        <span className="flex items-center gap-1.5">
          <Spinner />
          Exporting...
        </span>
      );
    }
    if (justDownloaded === format) return "Downloaded ✓";
    return idle;
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => handleExport("docx")}
          disabled={disabled}
          className="rounded-md bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-white dark:text-zinc-900"
        >
          {label("docx", "Export Word")}
        </button>
        <button
          type="button"
          onClick={() => handleExport("pptx")}
          disabled={disabled}
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-xs font-medium text-zinc-900 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-white dark:hover:bg-zinc-800"
        >
          {label("pptx", "Export PowerPoint")}
        </button>
        <button
          type="button"
          onClick={() => handleExport("pdf")}
          disabled={disabled}
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-xs font-medium text-zinc-900 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-white dark:hover:bg-zinc-800"
        >
          {label("pdf", "Export PDF")}
        </button>
      </div>
      {justDownloaded && (
        <p className="max-w-xs text-right text-xs text-emerald-600 dark:text-emerald-400">
          تم تنزيل الملف إلى مجلد Downloads
        </p>
      )}
      {errorMessage && (
        <p className="max-w-xs text-right text-xs text-red-600">{errorMessage}</p>
      )}
    </div>
  );
}

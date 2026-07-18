"use client";

import { useRef } from "react";
import { extractDocument } from "@/lib/api";
import { useDocumentStore } from "@/store/useDocumentStore";

export function UploadPanel() {
  const inputRef = useRef<HTMLInputElement>(null);
  const status = useDocumentStore((state) => state.status);
  const errorMessage = useDocumentStore((state) => state.errorMessage);
  const setFile = useDocumentStore((state) => state.setFile);
  const setDocument = useDocumentStore((state) => state.setDocument);
  const setStatus = useDocumentStore((state) => state.setStatus);
  const setError = useDocumentStore((state) => state.setError);

  async function handleFileSelected(file: File) {
    setFile(file);
    setStatus("uploading");
    try {
      const document = await extractDocument(file);
      setDocument(document);
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "تعذّر استخراج المستند";
      setError(message);
    }
  }

  return (
    <div className="flex h-full flex-col items-center justify-center gap-4">
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        disabled={status === "uploading"}
        className="rounded-lg bg-zinc-900 px-6 py-3 text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-white dark:text-zinc-900"
      >
        {status === "uploading" ? "جارٍ الاستخراج..." : "ارفع ملف PDF"}
      </button>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) handleFileSelected(file);
          e.target.value = "";
        }}
      />
      {status === "error" && (
        <p className="max-w-md text-center text-sm text-red-600">{errorMessage}</p>
      )}
    </div>
  );
}

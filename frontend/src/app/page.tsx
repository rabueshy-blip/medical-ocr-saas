"use client";

import { useState } from "react";
import type { Editor } from "@tiptap/react";
import { EditorPane } from "@/components/EditorPane";
import { ExportButton } from "@/components/ExportButton";
import { MediaLibrary } from "@/components/MediaLibrary";
import { PdfPane } from "@/components/PdfPane";
import { UploadPanel } from "@/components/UploadPanel";
import { WhatsAppButton } from "@/components/WhatsAppButton";
import { useDocumentStore } from "@/store/useDocumentStore";

export default function Home() {
  const file = useDocumentStore((state) => state.file);
  const document = useDocumentStore((state) => state.document);
  const [editor, setEditor] = useState<Editor | null>(null);

  const ready = file && document;

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center justify-between border-b border-zinc-200 bg-white px-4 py-2 dark:border-zinc-800 dark:bg-zinc-950">
        <h1 className="text-sm font-semibold">Medflow.ai</h1>
        <div className="flex items-center gap-3">
          {document && (
            <span className="text-xs text-zinc-500">{document.file_name}</span>
          )}
          {ready && <ExportButton editor={editor} />}
        </div>
      </header>

      <main className="flex-1 overflow-hidden">
        {!ready ? (
          <UploadPanel />
        ) : (
          <div className="grid h-full grid-cols-[1fr_1fr_220px]">
            <EditorPane onEditorReady={setEditor} />
            <PdfPane file={file} />
            <MediaLibrary />
          </div>
        )}
      </main>

      <WhatsAppButton />
    </div>
  );
}

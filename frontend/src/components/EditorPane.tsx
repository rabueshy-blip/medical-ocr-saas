"use client";

import { useEditor, EditorContent, type Editor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { TableRow, TableCell, TableHeader } from "@tiptap/extension-table";
import Image from "@tiptap/extension-image";
import { useEffect } from "react";
import {
  LocatableHeading,
  LocatableParagraph,
  LocatableTable,
} from "@/lib/tiptapBlockExtensions";
import { documentToTiptapContent } from "@/lib/documentToTiptap";
import { useDocumentStore } from "@/store/useDocumentStore";
import type { BoundingBox } from "@/lib/api";

export function EditorPane({ onEditorReady }: { onEditorReady?: (editor: Editor) => void }) {
  const document = useDocumentStore((state) => state.document);
  const setActiveBlock = useDocumentStore((state) => state.setActiveBlock);

  const editor = useEditor({
    immediatelyRender: false,
    extensions: [
      StarterKit.configure({ paragraph: false, heading: false }),
      LocatableParagraph,
      LocatableHeading,
      LocatableTable.configure({ resizable: false }),
      TableRow,
      TableHeader,
      TableCell,
      Image,
    ],
    editorProps: {
      attributes: {
        class:
          "prose prose-sm max-w-none focus:outline-none min-h-full p-6 " +
          "prose-table:border prose-td:border prose-th:border prose-td:p-2 prose-th:p-2",
      },
    },
    content: { type: "doc", content: [{ type: "paragraph" }] },
  });

  useEffect(() => {
    if (!editor || !document) return;
    editor.commands.setContent(documentToTiptapContent(document));
  }, [editor, document]);

  useEffect(() => {
    if (editor) onEditorReady?.(editor);
  }, [editor, onEditorReady]);

  function handleContainerClick(event: React.MouseEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement;
    const located = target.closest<HTMLElement>("[data-page]");
    if (!located) return;
    const page = Number(located.getAttribute("data-page"));
    const bboxRaw = located.getAttribute("data-bbox");
    if (!page || !bboxRaw) return;
    try {
      const bbox = JSON.parse(bboxRaw) as BoundingBox;
      setActiveBlock({ page, bbox });
    } catch {
      // ignore malformed bbox data
    }
  }

  function handleContainerDrop(event: React.DragEvent<HTMLDivElement>) {
    const src = event.dataTransfer.getData("application/x-medflow-image");
    if (!src || !editor) return;
    event.preventDefault();
    const coords = { left: event.clientX, top: event.clientY };
    const pos = editor.view.posAtCoords(coords)?.pos;
    if (pos == null) return;
    editor.chain().focus().insertContentAt(pos, { type: "image", attrs: { src } }).run();
  }

  return (
    <div
      className="h-full overflow-y-auto border-e border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950"
      onClick={handleContainerClick}
      onDragOver={(event) => event.preventDefault()}
      onDrop={handleContainerDrop}
    >
      <EditorContent editor={editor} className="h-full" />
    </div>
  );
}

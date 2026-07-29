import { create } from "zustand";
import type { BoundingBox, Document } from "@/lib/api";

export interface ActiveBlock {
  page: number;
  bbox: BoundingBox;
}

export interface ExtractionProgress {
  page: number;
  total: number;
}

interface DocumentState {
  file: File | null;
  document: Document | null;
  status: "idle" | "uploading" | "ready" | "error";
  errorMessage: string | null;
  activeBlock: ActiveBlock | null;
  progress: ExtractionProgress | null;
  setFile: (file: File | null) => void;
  setDocument: (document: Document) => void;
  setStatus: (status: DocumentState["status"]) => void;
  setError: (message: string) => void;
  setActiveBlock: (block: ActiveBlock | null) => void;
  setProgress: (progress: ExtractionProgress | null) => void;
  reset: () => void;
}

export const useDocumentStore = create<DocumentState>((set) => ({
  file: null,
  document: null,
  status: "idle",
  errorMessage: null,
  activeBlock: null,
  progress: null,
  setFile: (file) => set({ file }),
  setDocument: (document) => set({ document, status: "ready", progress: null }),
  setStatus: (status) => set({ status }),
  setError: (errorMessage) => set({ errorMessage, status: "error", progress: null }),
  setActiveBlock: (activeBlock) => set({ activeBlock }),
  setProgress: (progress) => set({ progress }),
  reset: () =>
    set({
      file: null,
      document: null,
      status: "idle",
      errorMessage: null,
      activeBlock: null,
      progress: null,
    }),
}));

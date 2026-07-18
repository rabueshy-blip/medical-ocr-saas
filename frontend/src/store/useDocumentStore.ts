import { create } from "zustand";
import type { BoundingBox, Document } from "@/lib/api";

export interface ActiveBlock {
  page: number;
  bbox: BoundingBox;
}

interface DocumentState {
  file: File | null;
  document: Document | null;
  status: "idle" | "uploading" | "ready" | "error";
  errorMessage: string | null;
  activeBlock: ActiveBlock | null;
  setFile: (file: File | null) => void;
  setDocument: (document: Document) => void;
  setStatus: (status: DocumentState["status"]) => void;
  setError: (message: string) => void;
  setActiveBlock: (block: ActiveBlock | null) => void;
  reset: () => void;
}

export const useDocumentStore = create<DocumentState>((set) => ({
  file: null,
  document: null,
  status: "idle",
  errorMessage: null,
  activeBlock: null,
  setFile: (file) => set({ file }),
  setDocument: (document) => set({ document, status: "ready" }),
  setStatus: (status) => set({ status }),
  setError: (errorMessage) => set({ errorMessage, status: "error" }),
  setActiveBlock: (activeBlock) => set({ activeBlock }),
  reset: () =>
    set({
      file: null,
      document: null,
      status: "idle",
      errorMessage: null,
      activeBlock: null,
    }),
}));

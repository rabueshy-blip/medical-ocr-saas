"use client";

import { useEffect, useState } from "react";
import {
  Viewer,
  Worker,
  type DocumentLoadEvent,
  type PdfJs,
  type RenderPage,
} from "@react-pdf-viewer/core";
import { pageNavigationPlugin } from "@react-pdf-viewer/page-navigation";
import { zoomPlugin } from "@react-pdf-viewer/zoom";
import "@react-pdf-viewer/core/lib/styles/index.css";
import "@react-pdf-viewer/page-navigation/lib/styles/index.css";
import "@react-pdf-viewer/zoom/lib/styles/index.css";
import { useDocumentStore } from "@/store/useDocumentStore";

interface PageDims {
  width: number;
  height: number;
}

export function PdfPane({ file }: { file: File }) {
  const activeBlock = useDocumentStore((state) => state.activeBlock);
  const pageNavigationPluginInstance = pageNavigationPlugin();
  const zoomPluginInstance = zoomPlugin();
  const { jumpToPage, GoToPreviousPage, GoToNextPage, CurrentPageInput, NumberOfPages } =
    pageNavigationPluginInstance;
  const { ZoomIn, ZoomOut, CurrentScale } = zoomPluginInstance;

  const [fileUrl, setFileUrl] = useState<string | null>(null);
  useEffect(() => {
    const url = URL.createObjectURL(file);
    setFileUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const [pdfDoc, setPdfDoc] = useState<PdfJs.PdfDocument | null>(null);
  const [pageDims, setPageDims] = useState<Record<number, PageDims>>({});

  function handleDocumentLoad(e: DocumentLoadEvent) {
    setPdfDoc(e.doc);
  }

  useEffect(() => {
    if (!activeBlock) return;
    jumpToPage(activeBlock.page - 1);

    if (pageDims[activeBlock.page] || !pdfDoc) return;
    pdfDoc.getPage(activeBlock.page).then((page) => {
      const viewport = page.getViewport({ scale: 1 });
      setPageDims((prev) => ({
        ...prev,
        [activeBlock.page]: { width: viewport.width, height: viewport.height },
      }));
    });
  }, [activeBlock, jumpToPage, pageDims, pdfDoc]);

  const renderPage: RenderPage = (props) => {
    const dims = pageDims[props.pageIndex + 1];
    const showHighlight =
      activeBlock && activeBlock.page - 1 === props.pageIndex && dims;

    return (
      <>
        {props.canvasLayer.children}
        {props.textLayer.children}
        {props.annotationLayer.children}
        {showHighlight && (
          <div
            style={{
              position: "absolute",
              left: `${(activeBlock.bbox.x0 / dims.width) * 100}%`,
              top: `${(activeBlock.bbox.y0 / dims.height) * 100}%`,
              width: `${((activeBlock.bbox.x1 - activeBlock.bbox.x0) / dims.width) * 100}%`,
              height: `${((activeBlock.bbox.y1 - activeBlock.bbox.y0) / dims.height) * 100}%`,
              background: "rgba(250, 204, 21, 0.35)",
              border: "2px solid #f59e0b",
              pointerEvents: "none",
            }}
          />
        )}
      </>
    );
  };

  return (
    <div className="flex h-full flex-col bg-zinc-100 dark:bg-zinc-900">
      <Worker workerUrl="/pdf.worker.min.js">
        <div className="flex items-center gap-3 border-b border-zinc-200 bg-white px-3 py-2 text-sm dark:border-zinc-800 dark:bg-zinc-950">
          <GoToPreviousPage />
          <span className="flex items-center gap-1">
            <CurrentPageInput /> / <NumberOfPages />
          </span>
          <GoToNextPage />
          <span className="ms-auto flex items-center gap-1">
            <ZoomOut />
            <CurrentScale>{(props) => <span>{Math.round(props.scale * 100)}%</span>}</CurrentScale>
            <ZoomIn />
          </span>
        </div>
        <div className="flex-1 overflow-hidden">
          {fileUrl && (
            <Viewer
              fileUrl={fileUrl}
              plugins={[pageNavigationPluginInstance, zoomPluginInstance]}
              renderPage={renderPage}
              onDocumentLoad={handleDocumentLoad}
            />
          )}
        </div>
      </Worker>
    </div>
  );
}

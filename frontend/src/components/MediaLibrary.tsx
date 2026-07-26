"use client";

import { imageAssetSrc } from "@/lib/api";
import { useDocumentStore } from "@/store/useDocumentStore";

/** يجمع صور/رسوم الصفحات الرقمية المستخرَجة، ليسحبها المترجم يدوياً ويُدرجها داخل
 * المحرر في مكانها الصحيح (EditorPane.tsx يستقبل الإفلات عبر editorProps.handleDrop). */
export function MediaLibrary() {
  const document = useDocumentStore((state) => state.document);
  const images = document?.images ?? [];

  return (
    <div className="flex h-full flex-col border-s border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
      <div className="border-b border-zinc-200 px-3 py-2 text-sm font-semibold dark:border-zinc-800">
        مكتبة الوسائط
      </div>
      <div className="flex-1 overflow-y-auto p-3">
        {images.length === 0 ? (
          <p className="text-xs text-zinc-500">لا توجد صور مستخرَجة في هذا المستند.</p>
        ) : (
          <div className="grid grid-cols-2 gap-2">
            {images.map((image) => {
              const src = imageAssetSrc(image);
              const extension = image.mime_type.split("/")[1] || "png";
              const fileName = `${image.image_id || `image_page_${image.page_number}`}.${extension}`;
              return (
                <div key={`${image.page_number}-${image.index}`} className="group relative">
                  <img
                    src={src}
                    alt={image.image_id || `صورة صفحة ${image.page_number}`}
                    draggable
                    onDragStart={(event) => {
                      event.dataTransfer.setData("application/x-medflow-image", src);
                      event.dataTransfer.setData("application/x-medflow-image-id", image.image_id);
                      event.dataTransfer.effectAllowed = "copy";
                    }}
                    className="cursor-grab rounded border border-zinc-200 object-contain dark:border-zinc-700"
                    title={image.image_id ? `${image.image_id} — صفحة ${image.page_number}` : `صفحة ${image.page_number}`}
                  />
                  <a
                    href={src}
                    download={fileName}
                    title="تحميل الصورة"
                    className="absolute end-1 top-1 rounded bg-black/60 p-1 text-white hover:bg-black/80"
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="h-3.5 w-3.5">
                      <path d="M12 3v12" />
                      <path d="M7 10l5 5 5-5" />
                      <path d="M5 21h14" />
                    </svg>
                  </a>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { extractDocumentStream } from "@/lib/api";
import { useDocumentStore } from "@/store/useDocumentStore";
import { WHATSAPP_NUMBER } from "@/components/WhatsAppButton";
import {
  addPagesUsed,
  applyUnlockCodeFromUrl,
  FREE_PAGE_LIMIT,
  getPagesUsed,
  getServerPagesUsed,
  isUnlimited,
  subscribePagesUsed,
} from "@/lib/usageLimit";

export function UploadPanel() {
  const inputRef = useRef<HTMLInputElement>(null);
  const status = useDocumentStore((state) => state.status);
  const errorMessage = useDocumentStore((state) => state.errorMessage);
  const progress = useDocumentStore((state) => state.progress);
  const setFile = useDocumentStore((state) => state.setFile);
  const setDocument = useDocumentStore((state) => state.setDocument);
  const setStatus = useDocumentStore((state) => state.setStatus);
  const setError = useDocumentStore((state) => state.setError);
  const setProgress = useDocumentStore((state) => state.setProgress);

  // getServerSnapshot (0) يُستخدَم فقط أثناء SSR/الرسم الأول (الخادم لا يملك
  // localStorage) — بعد hydration في المتصفح يُقرأ الرصيد الحقيقي مباشرة، ويعاد
  // تقييمه تلقائياً كلما استدعت addPagesUsed حدث التغيير.
  const pagesUsed = useSyncExternalStore(
    subscribePagesUsed,
    getPagesUsed,
    getServerPagesUsed,
  );

  // رابط سري (?unlock=...) يُطبَّق مرة واحدة عند التحميل ويُخزَّن دائماً في هذا
  // المتصفح — استثناء يدوي لعملاء محدَّدين بلا نظام حسابات في الموقع.
  const [unlimited, setUnlimited] = useState(false);
  useEffect(() => {
    applyUnlockCodeFromUrl();
    setUnlimited(isUnlimited());
  }, []);

  const limitReached = !unlimited && pagesUsed >= FREE_PAGE_LIMIT;

  async function handleFileSelected(file: File) {
    if (!isUnlimited() && getPagesUsed() >= FREE_PAGE_LIMIT) return;
    setFile(file);
    setStatus("uploading");
    setProgress(null);
    try {
      const document = await extractDocumentStream(file, (page, total) => {
        setProgress({ page, total });
      });
      setDocument(document);
      addPagesUsed(document.pages.length);
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "تعذّر استخراج المستند";
      setError(message);
    }
  }

  if (limitReached) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center">
        <p className="max-w-md text-zinc-700 dark:text-zinc-300">
          لقد وصلت إلى الحد الأقصى للخطة المجانية (30 صفحة). للاستمرار في استخدام
          الخدمة، تواصل معنا عبر واتساب للاشتراك.
        </p>
        <a
          href={`https://wa.me/${WHATSAPP_NUMBER}`}
          target="_blank"
          rel="noopener noreferrer"
          className="rounded-lg bg-[#25D366] px-6 py-3 text-white hover:opacity-90"
        >
          تواصل معنا للاشتراك
        </a>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col items-center justify-center gap-4">
      <p className="text-center text-zinc-700 dark:text-zinc-300">
        Upload Medical report, research, lecture, file, etc.
      </p>
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        disabled={status === "uploading"}
        className="rounded-lg bg-zinc-900 px-6 py-3 text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-white dark:text-zinc-900"
      >
        {status === "uploading"
          ? progress
            ? `جارٍ الاستخراج... (${progress.page}/${progress.total})`
            : "جارٍ الاستخراج..."
          : "Try free (Up to 30 pages)"}
      </button>
      {status === "uploading" && (
        <div className="w-64">
          <div className="h-2 w-full overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
            <div
              className="h-full rounded-full bg-zinc-900 transition-all duration-300 dark:bg-white"
              style={{
                width: progress ? `${(progress.page / progress.total) * 100}%` : "8%",
              }}
            />
          </div>
          {progress && (
            <p className="mt-1 text-center text-xs text-zinc-500">
              صفحة {progress.page} من {progress.total}
            </p>
          )}
        </div>
      )}
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        // "hidden" (display:none) بدل هذا كان يمنع Safari من فتح نافذة اختيار
        // الملف فعلياً عند استدعاء .click() برمجياً (خلل WebKit معروف: عناصر
        // display:none لا تستجيب لـ.click() المُستدعى من كود، رغم عملها في
        // Chrome/Firefox) — sr-only تُبقي العنصر في الشجرة بصرياً (opacity:0 +
        // موضع مطلق) بدل إخفائه بالكامل، فيعمل في كل المتصفحات.
        className="sr-only"
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

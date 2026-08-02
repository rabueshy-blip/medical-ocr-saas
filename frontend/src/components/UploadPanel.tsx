"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { extractDocumentStream, extractPptx } from "@/lib/api";
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
      // ملفات PowerPoint (.pptx) تُستخرَج بنيوياً مباشرة (لا OCR/LM)، بلا حاجة
      // لتقدّم مرحلي كما في PDF الممسوح — طلب واحد يكفي.
      const document = file.name.toLowerCase().endsWith(".pptx")
        ? await extractPptx(file)
        : await extractDocumentStream(file, (page, total) => {
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
          لقد وصلت إلى الحد الأقصى للخطة المجانية ({FREE_PAGE_LIMIT} صفحة). للاستمرار في
          استخدام الخدمة، تواصل معنا عبر واتساب للاشتراك.
        </p>
        <a
          href={`https://wa.me/${WHATSAPP_NUMBER}`}
          target="_blank"
          rel="noopener noreferrer"
          className="rounded-lg bg-[#25D366] px-6 py-3 text-white hover:opacity-90"
        >
          Contact us to subscribe
        </a>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col items-center justify-center gap-4">
      <div className="mb-2 flex flex-col items-center gap-3 sm:flex-row sm:items-start sm:justify-center">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src="/before-after.jpg"
          alt="Medflow: before and after comparison"
          className="w-full max-w-sm rounded-lg sm:max-w-[240px]"
        />
        <div className="flex flex-col items-center gap-2">
          <p className="text-center text-sm font-medium text-emerald-600 dark:text-emerald-400">
            New feature added
          </p>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/extracted-images-feature.jpg"
            alt="New feature: extracted images are now included in your downloaded file"
            className="w-full max-w-[96px] rounded-lg sm:max-w-[240px]"
          />
        </div>
      </div>
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
            ? `Extracting... (${progress.page}/${progress.total})`
            : "Extracting..."
          : `Try free (Up to ${FREE_PAGE_LIMIT} pages)`}
      </button>
      {status !== "uploading" && (
        // ملاحظة وقائية تحت الزر: خُفِّض الحد من 30 إلى 20 صفحة (طلب مباشر من المستخدم
        // بعد فشل حقيقي مؤكَّد على الإنتاج عند 38 صفحة) — ينطبق حتى على مستخدمي unlock
        // غير المقيَّدين بـ FREE_PAGE_LIMIT نفسه.
        <p className="max-w-sm text-center text-xs text-zinc-500 dark:text-zinc-500">
          If your file is over {FREE_PAGE_LIMIT} pages, we recommend splitting it into
          smaller parts first.
        </p>
      )}
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
        accept="application/pdf,.pptx,application/vnd.openxmlformats-officedocument.presentationml.presentation"
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

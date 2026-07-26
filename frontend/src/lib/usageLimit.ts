const STORAGE_KEY = "medflow_pages_used";
const CHANGE_EVENT = "medflow-pages-used-changed";
const UNLIMITED_KEY = "medflow_unlimited";
const UNLOCK_CODE = "f835811c4361361504358ed5";

/** حد الخطة المجانية: مجموع تراكمي لعدد صفحات كل الملفات التي رفعها نفس الزائر عبر
 * كل الجلسات (مُتتبَّع في localStorage للمتصفح — لا يوجد نظام حسابات/تسجيل دخول في
 * الموقع حالياً، فهذا أبسط تتبّع ممكن). قرار مستخدم صريح: يمكن تجاوزه بمسح بيانات
 * المتصفح، مقبول لمرحلة المشروع الحالية. */
export const FREE_PAGE_LIMIT = 30;

export function getPagesUsed(): number {
  if (typeof window === "undefined") return 0;
  const raw = window.localStorage.getItem(STORAGE_KEY);
  const parsed = raw ? Number(raw) : 0;
  return Number.isFinite(parsed) ? parsed : 0;
}

/** تُطلق CHANGE_EVENT بعد الكتابة لأن حدث "storage" القياسي لا يصل إلا للتبويبات
 * الأخرى (وليس التبويب الذي أجرى التغيير نفسه) — `useSyncExternalStore` في
 * `UploadPanel` يحتاج إشعاراً فورياً في نفس التبويب لإعادة القراءة بعد كل رفع. */
export function addPagesUsed(count: number): number {
  const total = getPagesUsed() + count;
  window.localStorage.setItem(STORAGE_KEY, String(total));
  window.dispatchEvent(new Event(CHANGE_EVENT));
  return total;
}

export function subscribePagesUsed(callback: () => void): () => void {
  window.addEventListener("storage", callback);
  window.addEventListener(CHANGE_EVENT, callback);
  return () => {
    window.removeEventListener("storage", callback);
    window.removeEventListener(CHANGE_EVENT, callback);
  };
}

export function getServerPagesUsed(): number {
  return 0;
}

/** لعملاء محدَّدين يُمنَح استثناء دائم من حد الصفحات المجانية عبر رابط سري
 * (`?unlock=...`) — لا يوجد نظام حسابات في الموقع، فهذا أبسط استثناء ممكن
 * دون تعديل الخادم. */
export function isUnlimited(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(UNLIMITED_KEY) === "true";
}

export function applyUnlockCodeFromUrl(): void {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams(window.location.search);
  const code = params.get("unlock");
  if (!code || code !== UNLOCK_CODE) return;

  window.localStorage.setItem(UNLIMITED_KEY, "true");
  params.delete("unlock");
  const query = params.toString();
  window.history.replaceState(
    {},
    "",
    window.location.pathname + (query ? `?${query}` : "") + window.location.hash,
  );
}

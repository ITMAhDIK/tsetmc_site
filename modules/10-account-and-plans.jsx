/* ============================================================================
 * MODULE 11 — 10-ACCOUNT-AND-PLANS
 * Responsibility: Account and plan presentation (local placeholder only).
 * Load order: after 00-runtime.jsx and in the numeric order used by index HTML.
 * Shared contracts: React hooks and declarations from earlier modules are
 * available in this page. Keep API payloads and business rules unchanged here.
 * Maintenance: add focused comments beside non-obvious calculations, state
 * transitions, data provenance assumptions, and event handlers.
 * ========================================================================== */
// --- 10. Page 7: Account & Plans (Local Placeholder) ---
    function AccountAndPlansPage({ plan, setPlan }) {
      return (
        <div className="space-y-3 text-xs">
          <div className="p-3 rounded border border-amber-500/30 bg-amber-500/10 text-amber-300">
            حالت اجرای محلی: سیستم احراز هویت ابری (Supabase) در نسخه ویندوز محلی غیرفعال است و تغییر سطح دسترسی زیر صرفاً نمایشی است.
          </div>
          <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] flex justify-between items-center">
            <div>
              <div className="font-bold text-sm">حساب کاربری محلی و سطح دسترسی</div>
              <div className="text-[var(--text-secondary)]">در اجرای محلی، تمام امکانات تحلیلی بدون محدودیت در دسترس شماست.</div>
            </div>
            <button onClick={() => setPlan(p => p === 'free' ? 'premium' : 'free')} className="px-3 py-1.5 rounded bg-blue-600 text-white">
              سطح نمایشی: {plan}
            </button>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="p-4 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-1.5">
              <h3 className="font-bold text-sm">طرح رایگان (Free)</h3>
              <p className="text-[var(--text-secondary)]">نمای بازار، صفحات پایه نمادها، محدودیت تاریخچه سهام‌داری و ۱ دیده‌بان.</p>
            </div>
            <div className="p-4 rounded border border-emerald-500/40 bg-[var(--bg-panel)] space-y-1.5">
              <h3 className="font-bold text-sm text-emerald-400">طرح حرفه‌ای (Premium)</h3>
              <p className="text-[var(--text-secondary)]">تاریخچه کامل، بازسازی پرتفوی چندشرکتی، غربالگر پیشرفته و خروجی اکسل/CSV.</p>
            </div>
          </div>
        </div>
      );
    }

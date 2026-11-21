/* ============================================================================
 * MODULE 08 — 07-PORTFOLIO-RECONSTRUCTION
 * Responsibility: Investor portfolio reconstruction across companies and dates.
 * Load order: after 00-runtime.jsx and in the numeric order used by index HTML.
 * Shared contracts: React hooks and declarations from earlier modules are
 * available in this page. Keep API payloads and business rules unchanged here.
 * Maintenance: add focused comments beside non-obvious calculations, state
 * transitions, data provenance assumptions, and event handlers.
 * ========================================================================== */
// --- 7. Page 4: Investor Portfolio Reconstruction ---
    function PortfolioReconstructionPage({ currency, theme, onOpenStock, dateMode, initialInvestor = '' }) {
      const [invQuery, setInvQuery] = useState('');
      const [invSort, setInvSort] = useState('linked');
      const [investors, setInvestors] = useState([]);
      const [selectedId, setSelectedId] = useState('');
      const [idInput, setIdInput] = useState('');
      const [asOfDate, setAsOfDate] = useState('');
      const [portfolio, setPortfolio] = useState(null);
      const [loading, setLoading] = useState(false);
      const [compQuery, setCompQuery] = useState('');
      const [compResults, setCompResults] = useState([]);
      const [compSymbol, setCompSymbol] = useState('');
      const [compHolders, setCompHolders] = useState([]);
      const [compLoading, setCompLoading] = useState(false);

      // The historical view is separate from the latest-position snapshot.
      // A symbol is added to this list if it appears at any time in the
      // identity-resolved holder history, including securities no longer
      // present in the latest disclosure.
      const [historySymbol, setHistorySymbol] = useState('');
      const [historyOffset, setHistoryOffset] = useState(0);
      const [historyData, setHistoryData] = useState(null);
      const [historyLoading, setHistoryLoading] = useState(false);
      const HISTORY_PAGE_SIZE = 100;

      // Open a specific investor (e.g. clicked on a holder on the
      // dashboard): the dashboard passes "scheme:id".
      useEffect(() => { if (initialInvestor) setSelectedId(initialInvestor); }, [initialInvestor]);

      // Direct ID entry: "share_id:123456" / "ondate_share_id:123456",
      // or a bare number (treated as share_id).
      const applyIdInput = () => {
        const v = idInput.trim();
        if (!v) return;
        setSelectedId(v.includes(':') ? v : `share_id:${v}`);
      };

      // Find an investor through a company: search the company, pick it,
      // and its shareholders appear below -- click one to load them.
      useEffect(() => {
        if (!compQuery.trim()) { setCompResults([]); return; }
        const timer = setTimeout(() => {
          apiFetch(`/api/v1/stocks/search?q=${encodeURIComponent(compQuery.trim())}&limit=8`)
            .then(res => setCompResults(res.results || []))
            .catch(() => {});
        }, 200);
        return () => clearTimeout(timer);
      }, [compQuery]);

      useEffect(() => {
        if (!compSymbol) { setCompHolders([]); return; }
        setCompLoading(true);
        apiFetch(`/api/v1/stocks/${encodeURIComponent(compSymbol)}/shareholders`)
          .then(res => setCompHolders(res.rows || []))
          .catch(() => setCompHolders([]))
          .finally(() => setCompLoading(false));
      }, [compSymbol]);

      useEffect(() => {
        const timer = setTimeout(() => {
          apiFetch(`/api/v1/portfolios/investors?q=${encodeURIComponent(invQuery)}&limit=30&sort_by=${invSort}`)
            .then(res => {
              const list = res.investors || [];
              setInvestors(list);
              if (!selectedId && list.length > 0) setSelectedId(list[0].id);
            });
        }, 200);
        return () => clearTimeout(timer);
      }, [invQuery, invSort]);

      useEffect(() => {
        if (!selectedId) return;
        setLoading(true);
        setHistorySymbol('');
        setHistoryOffset(0);
        setHistoryData(null);
        const qs = asOfDate ? `?as_of_date=${encodeURIComponent(asOfDate)}` : '';
        apiFetch(`/api/v1/portfolios/${encodeURIComponent(selectedId)}${qs}`)
          .then(setPortfolio)
          .finally(() => setLoading(false));
      }, [selectedId, asOfDate]);

      // Large investors can have thousands of daily observations in one
      // security. Fetch the selected security's ledger in bounded pages.
      useEffect(() => {
        if (!selectedId || !historySymbol) {
          setHistoryData(null);
          return;
        }
        let active = true;
        setHistoryLoading(true);
        const params = new URLSearchParams({
          symbol: historySymbol,
          limit: String(HISTORY_PAGE_SIZE),
          offset: String(historyOffset),
        });
        if (asOfDate) params.set('as_of_date', asOfDate);
        apiFetch(`/api/v1/portfolios/${encodeURIComponent(selectedId)}/history?${params.toString()}`)
          .then(result => { if (active) setHistoryData(result); })
          .catch(() => { if (active) setHistoryData({ rows: [], total: 0, error: true }); })
          .finally(() => { if (active) setHistoryLoading(false); });
        return () => { active = false; };
      }, [selectedId, historySymbol, historyOffset, asOfDate]);

      const sum = portfolio?.summary || {};
      const chartSeries = useMemo(() => {
        // Values in همر (1e12 Rial) in IRR mode, همت (1e12 Toman = 1e13 Rial)
        // in IRT mode.
        const divisor = currency === 'IRT' ? 1e13 : 1e12;
        return (portfolio?.history_series || [])
          .filter(p => p.combined_value_rial !== null && p.combined_value_rial !== undefined)
          .map(p => ({ time: p.time, value: p.combined_value_rial / divisor }));
      }, [portfolio, currency]);

      // Labels explicitly describe observed quantities. They must never
      // imply that snapshots prove a specific buy/sell execution.
      const historyChangeLabel = (row) => ({
        capital_action: 'رویداد سرمایه‌ای',
        first_observation: 'اولین مشاهده؛ سابقهٔ قبلی در دسترس نیست',
        not_disclosed: 'در این تاریخ افشا نشده',
        change_after_gap: 'تغییر پس از فاصلهٔ مشاهدات',
        net_increase: 'افزایش خالص مشاهده‌شده؛ لزوماً خرید نیست',
        net_decrease: 'کاهش خالص مشاهده‌شده؛ لزوماً فروش نیست',
        increase_unquantified: 'افزایش ثبت شده؛ مقدار نامشخص',
        decrease_unquantified: 'کاهش ثبت شده؛ مقدار نامشخص',
        identity_conflict: 'تعارض شناسه‌های پیوندخورده',
        observation: 'مشاهدهٔ موجودی',
      })[row.change_classification] || row.change_type || 'نامشخص';

      return (
        <div className="space-y-3 text-xs">      return (
        <div className="space-y-3 text-xs">
          <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] flex flex-wrap justify-between items-center gap-2">
            <div>
              <h1 className="text-sm font-bold">بازسازی پرتفوی سرمایه‌گذاران</h1>
              <p className="text-[11px] text-[var(--text-secondary)]">هویت هر سرمایه‌گذار از شناسه‌های (id_scheme, shareholder_id) منبع ساخته می‌شود؛ لینک‌های «محتمل» فقط وقتی در جمع می‌آیند که در همهٔ تاریخ‌ها تاریخ+سهام+نام دقیقاً یکسان بوده و هیچ تناقضی نداشته باشند.</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <input value={invQuery} onChange={e => setInvQuery(e.target.value)} placeholder="جستجوی سرمایه‌گذار..." className="px-2.5 py-1.5 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] w-48" />
              <select value={invSort} onChange={e => setInvSort(e.target.value)} className="px-2.5 py-1.5 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)]" title="مرتب‌سازی فهرست سرمایه‌گذاران">
                <option value="linked">بیشترین رکورد</option>
                <option value="inferred">بیشترین لینک استنتاجی</option>
              </select>
              <select value={selectedId} onChange={e => setSelectedId(e.target.value)} className="px-2.5 py-1.5 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] max-w-xs">
                {investors.map(inv => (
                  <option key={inv.id} value={inv.id}>
                    {inv.canonical_name} ({toFa(inv.verified_links_count)} تأیید / {toFa(inv.inferred_links_count || 0)} استنتاجی / {toFa(inv.linked_records_count)} کل)
                  </option>
                ))}
              </select>
              <input value={idInput} onChange={e => setIdInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') applyIdInput(); }} placeholder="شناسه: share_id:123456" className="px-2.5 py-1.5 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] w-48 num" title="شناسهٔ سرمایه‌گذار به شکل scheme:id — عدد خالی یعنی share_id" />
              <button onClick={applyIdInput} className="px-3 py-1.5 rounded bg-blue-600 text-white font-bold">اعمال</button>
              <input type="date" value={asOfDate} onChange={e => setAsOfDate(e.target.value)} className="px-2 py-1.5 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] num" title="تاریخ میلادی مبنا" />
              {asOfDate && <span className="text-[11px] text-[var(--text-secondary)] num">{fmtGregorianDate(asOfDate)}</span>}
            </div>
          </div>

          <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-2">
            <div className="font-bold">یافتن سرمایه‌گذار از روی شرکت</div>
            <div className="flex flex-wrap gap-2">
              <input value={compQuery} onChange={e => { setCompQuery(e.target.value); setCompSymbol(''); }} placeholder="نام یا نماد شرکت را جستجو کنید..." className="px-2.5 py-1.5 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] w-64" />
              {compResults.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {compResults.map(r => (
                    <button key={r.symbol} onClick={() => { setCompSymbol(r.symbol); setCompQuery(r.symbol); setCompResults([]); }} className={`px-2.5 py-1.5 rounded border border-[var(--border-subtle)] ${compSymbol === r.symbol ? 'bg-blue-600 text-white' : 'bg-[var(--bg-elevated)]'}`}>{r.symbol} — {r.company_name}</button>
                  ))}
                </div>
              )}
            </div>
            {compSymbol && (
              <div className="space-y-1">
                <div className="text-[var(--text-secondary)]">سهام‌داران عمدهٔ {compSymbol} — روی یک سهام‌دار کلیک کنید تا پرتفوی او باز شود:</div>
                {compLoading ? (
                  <div className="text-[var(--text-secondary)]">در حال واکشی سهام‌داران...</div>
                ) : compHolders.length === 0 ? (
                  <div className="text-[var(--text-secondary)]">سهام‌داری برای این نماد ثبت نشده است.</div>
                ) : (
                  <div className="max-h-56 overflow-y-auto divide-y divide-[var(--border-subtle)]">
                    {compHolders.map(h => (
                      <div key={`${h.id_scheme}:${h.shareholder_id}`} onClick={() => setSelectedId(`${h.id_scheme}:${h.shareholder_id}`)} className="py-1.5 flex justify-between gap-2 cursor-pointer hover:text-blue-400">
                        <span className="font-semibold">{h.shareholder_name} <span className="text-[10px] text-[var(--text-secondary)] num">{h.id_scheme}:{h.shareholder_id}</span></span>
                        <span className="num">{fmtNum(h.shares)} سهم ({fmtPct(h.ownership_percent_calculated)})</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          {loading ? (
            <div className="p-8 text-center text-[var(--text-secondary)]">در حال بازسازی پرتفوی...</div>
          ) : portfolio && (
            <>
              <div className="grid grid-cols-1 md:grid-cols-4 gap-2.5">
                <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
                  <div className="text-[var(--text-secondary)]">ارزش کل تأییدشده (ترکیبی)</div>
                  <div className="text-base font-bold num text-emerald-400 mt-1">{fmtMoney(sum.confirmed_combined_value_rial, currency)}</div>
                  <div className="text-[10px] text-[var(--text-secondary)] mt-0.5">فاقد قیمت معتبر: {toFa(sum.positions_missing_price_count)} پوزیشن</div>
                </div>
                <div className="p-3 rounded border border-blue-500/30 bg-blue-500/5">
                  <div className="text-blue-300">ارزش محتمل (تطبیق دقیق استنتاجی)</div>
                  <div className="text-base font-bold num text-blue-400 mt-1">{fmtMoney(sum.probable_combined_value_rial, currency)}</div>
                  <div className="text-[10px] text-[var(--text-secondary)] mt-0.5">شرکت‌های محتمل: {toFa(sum.probable_companies_count)}</div>
                </div>
                <div className="p-3 rounded border border-amber-500/30 bg-amber-500/5">
                  <div className="text-amber-300">کاندیداهای غیرقطعی (مستثنی)</div>
                  <div className="text-base font-bold num mt-1">{toFa(sum.uncertain_candidates_count)}</div>
                </div>
                <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
                  <div className="text-[var(--text-secondary)]">وضعیت سرمایه‌گذار</div>
                  <div className="mt-1"><IdentityBadge status={portfolio.investor.verification_status} /></div>
                </div>
              </div>

              {portfolio.identity_evidence && (
                <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-1.5">
                  <div className="font-bold">چرا هویت این سرمایه‌گذار درست است؟ (شواهد)</div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1">
                    <div>وضعیت در رجیستری هویت: <IdentityBadge status={portfolio.identity_evidence.registry_identity_status} /></div>
                    <div>نوع سهام‌دار: {portfolio.identity_evidence.shareholder_type === 'legal' ? 'حقوقی' : portfolio.identity_evidence.shareholder_type === 'individual' ? 'حقیقی' : '—'}</div>
                    <div>اولین مشاهده: <span className="num">{fmtDate(portfolio.identity_evidence.first_seen_date_jalali)}</span> — آخرین مشاهده: <span className="num">{fmtDate(portfolio.identity_evidence.last_seen_date_jalali)}</span></div>
                    <div>رکوردهای پیوندخورده: <span className="num">{toFa(portfolio.identity_evidence.linked_records_count)}</span> (تأییدشده: <span className="num">{toFa(portfolio.identity_evidence.verified_links_count)}</span>، استنتاجی: <span className="num">{toFa(portfolio.identity_evidence.inferred_links_count)}</span>)</div>
                    <div>پوزیشن‌ها بر اساس سطح اطمینان — تأییدشده: <span className="num">{toFa(portfolio.identity_evidence.positions_by_tier?.confirmed || 0)}</span>، محتمل: <span className="num">{toFa(portfolio.identity_evidence.positions_by_tier?.probable || 0)}</span>، غیرقطعی: <span className="num">{toFa(portfolio.identity_evidence.positions_by_tier?.uncertain || 0)}</span></div>
                  </div>
                  <div className="text-[11px] text-[var(--text-secondary)]">
                    «تأییدشده» یعنی پوزیشن رکورد مستقیم همین شناسه است (یا منبع خودش هر دو شناسه را در یک ردیف گذاشته است). «محتمل» یعنی پوزیشن زیر یک شناسهٔ دوقلوی استنتاجی است: در همهٔ تاریخ‌ها تاریخ + تعداد سهام + نام دقیقاً یکسان بوده، فقط یک کاندید وجود داشته، هیچ تناقضی در هیچ تاریخی نبوده، و برای اشخاص حقیقیِ بدون نام (شخص حقیقی) حداقل دو تاریخ مستقل هم‌خوانی داشته‌اند؛ نام فقط برای بررسی سازگاری استفاده شده، نه به‌تنهایی. بقیهٔ رکوردها «غیرقطعی» هستند و در هیچ جمعی نمی‌آیند.
                  </div>
                </div>
              )}

              {portfolio.historical_summary && (
                <section className="rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] overflow-hidden">
                  <div className="p-3 border-b border-[var(--border-subtle)]">
                    <h3 className="font-bold text-sm">پرتفوی تاریخی؛ تمام نمادهای مشاهده‌شده از ابتدا</h3>
                    <p className="mt-1 text-[11px] text-[var(--text-secondary)]">
                      نمادهایی که در گذشته وجود داشته‌اند حتی اگر در آخرین فهرست سهام‌داران نباشند، در اینجا باقی می‌مانند. تاریخ مشاهده، تاریخ معامله نیست.
                    </p>
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-5 gap-2 p-3">
                    <div className="rounded bg-[var(--bg-elevated)] p-2"><div className="text-[10px] text-[var(--text-secondary)]">نمادهای تاریخی</div><div className="font-bold text-base num">{toFa(portfolio.historical_summary.symbols_count)}</div></div>
                    <div className="rounded bg-[var(--bg-elevated)] p-2"><div className="text-[10px] text-[var(--text-secondary)]">کل مشاهدات</div><div className="font-bold text-base num">{toFa(portfolio.historical_summary.observations_count)}</div></div>
                    <div className="rounded bg-[var(--bg-elevated)] p-2"><div className="text-[10px] text-[var(--text-secondary)]">رویدادهای تغییریافته</div><div className="font-bold text-base num">{toFa(portfolio.historical_summary.change_events_count)}</div></div>
                    <div className="rounded bg-[var(--bg-elevated)] p-2"><div className="text-[10px] text-[var(--text-secondary)]">با تعداد سهم معتبر</div><div className="font-bold text-base num">{toFa(portfolio.historical_summary.known_shares_observations_count)}</div></div>
                    <div className="rounded bg-[var(--bg-elevated)] p-2"><div className="text-[10px] text-[var(--text-secondary)]">بدون تعداد معتبر</div><div className="font-bold text-base num">{toFa(portfolio.historical_summary.unavailable_shares_observations_count)}</div></div>
                  </div>
                  <div className="px-3 pb-2 text-[10px] text-[var(--text-secondary)]">
                    بازهٔ داده: {fmtDate(portfolio.historical_summary.first_observation_date_jalali)} تا {fmtDate(portfolio.historical_summary.last_observation_date_jalali)}.
                    افزایش/کاهش، تغییر خالص موجودی افشاشده است؛ نه اثبات خرید یا فروش.
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-right">
                      <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                        <tr><th className="p-2">نماد</th><th className="p-2">اولین مشاهده</th><th className="p-2">آخرین مشاهده</th><th className="p-2">اولین تعداد</th><th className="p-2">تعداد در آخرین مشاهده</th><th className="p-2">آخرین تعداد معلوم</th><th className="p-2">بیشترین تعداد</th><th className="p-2">افزایش / کاهش</th><th className="p-2">تعداد رکورد</th></tr>
                      </thead>
                      <tbody className="divide-y divide-[var(--border-subtle)]">
                        {(portfolio.historical_symbols || []).length === 0 ? (
                          <tr><td colSpan="9" className="p-4 text-center text-[var(--text-secondary)]">تاریخچه‌ای برای شناسه‌های پیوندخورده پیدا نشد.</td></tr>
                        ) : portfolio.historical_symbols.map(item => (
                          <tr key={item.ins_code} className={historySymbol === item.symbol ? 'bg-blue-500/10' : 'hover:bg-[var(--bg-elevated)]'}>
                            <td className="p-2 whitespace-nowrap">
                              <button className="font-bold text-blue-400" onClick={() => { setHistorySymbol(item.symbol); setHistoryOffset(0); }}>{item.symbol}</button>
                              <button className="mr-2 text-[10px] text-[var(--text-secondary)] underline" onClick={() => onOpenStock(item.symbol)}>جزئیات نماد</button>
                            </td>
                            <td className="p-2 num whitespace-nowrap">{fmtDate(item.first_observation_date_jalali)}</td>
                            <td className="p-2 num whitespace-nowrap">{fmtDate(item.last_observation_date_jalali)}</td>
                            <td className="p-2 num">{item.first_observed_shares === null ? '—' : fmtNum(item.first_observed_shares)}</td>
                            <td className="p-2 num">{item.last_observation_shares === null ? 'افشا نشده' : fmtNum(item.last_observation_shares)}</td>
                            <td className="p-2 num">{item.last_known_shares === null ? '—' : fmtNum(item.last_known_shares)}</td>
                            <td className="p-2 num">{item.max_observed_shares === null ? '—' : fmtNum(item.max_observed_shares)}</td>
                            <td className="p-2 num whitespace-nowrap">{toFa(item.increase_events_count)} / {toFa(item.decrease_events_count)}</td>
                            <td className="p-2 num">{toFa(item.observations_count)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              )}

              {historySymbol && (
                <section className="rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] overflow-hidden">
                  <div className="p-3 border-b border-[var(--border-subtle)] flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <h3 className="font-bold text-sm">دفتر تاریخی {historySymbol}</h3>
                      <p className="text-[10px] text-[var(--text-secondary)] mt-1">قیمت مرجع/پایانی است و قیمت اجرای معامله محسوب نمی‌شود.</p>
                    </div>
                    <button className="px-2 py-1 rounded border border-[var(--border-subtle)]" onClick={() => { setHistorySymbol(''); setHistoryData(null); }}>بستن تاریخچه</button>
                  </div>
                  {historyLoading ? (
                    <div className="p-5 text-center text-[var(--text-secondary)]">در حال دریافت دفتر تاریخچه...</div>
                  ) : historyData?.error ? (
                    <div className="p-5 text-center text-rose-400">دریافت تاریخچه ممکن نشد. ابتدا بازسازی sidecar نسخهٔ ۱۰ باید کامل شود.</div>
                  ) : (
                    <>
                      <div className="p-2 text-[10px] text-[var(--text-secondary)]">{toFa(historyData?.total || 0)} مشاهده — صفحهٔ {toFa(Math.floor(historyOffset / HISTORY_PAGE_SIZE) + 1)}</div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-right">
                          <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                            <tr><th className="p-2">تاریخ</th><th className="p-2">نوع تغییر</th><th className="p-2">موجودی قبلی</th><th className="p-2">موجودی مشاهده‌شده</th><th className="p-2">تغییر خالص</th><th className="p-2">قیمت مرجع</th><th className="p-2">تاریخ قیمت</th><th className="p-2">ارزش تخمینی موجودی</th><th className="p-2">رویداد سرمایه‌ای</th><th className="p-2">فاصله (روز معاملاتی)</th><th className="p-2">وضعیت داده/هویت</th></tr>
                          </thead>
                          <tbody className="divide-y divide-[var(--border-subtle)]">
                            {(historyData?.rows || []).length === 0 ? (
                              <tr><td colSpan="11" className="p-4 text-center text-[var(--text-secondary)]">رکوردی برای این نماد پیدا نشد.</td></tr>
                            ) : historyData.rows.map((row,index) => (
                              <tr key={`${row.date}-${index}`} className={row.identity_conflict ? 'bg-amber-500/10' : ''}>
                                <td className="p-2 num whitespace-nowrap">{fmtDate(row.date_jalali)}</td>
                                <td className="p-2 whitespace-nowrap">{historyChangeLabel(row)}</td>
                                <td className="p-2 num">{row.previous_shares === null ? '—' : fmtNum(row.previous_shares)}</td>
                                <td className="p-2 num">{row.shares === null ? 'افشا نشده/نامعتبر' : fmtNum(row.shares)}</td>
                                <td className="p-2 num">{row.shares_change === null ? '—' : fmtNum(row.shares_change)}</td>
                                <td className="p-2 num">{row.reference_price === null ? '—' : fmtNum(currency === 'IRT' ? row.reference_price / 10 : row.reference_price)}</td>
                                <td className="p-2 num whitespace-nowrap">{fmtDate(row.reference_price_date_jalali)}</td>
                                <td className="p-2 num">{row.estimated_holding_value_rial === null ? 'بدون قیمت معتبر' : fmtMoney(row.estimated_holding_value_rial,currency)}</td>
                                <td className="p-2 whitespace-nowrap">{row.corporate_action ? `${row.corporate_action} ${row.corporate_action_ratio ? '(' + row.corporate_action_ratio + ')' : ''}` : '—'}</td>
                                <td className="p-2 num">{row.gap_trading_days === null ? '—' : toFa(row.gap_trading_days)}</td>
                                <td className="p-2 text-[10px] min-w-40">
                                  {row.identity_conflict ? 'تعارض شناسه؛ از ارزش‌گذاری حذف شد' : row.change_calculation_status || row.valuation_status || '—'}
                                  {row.note ? <div className="text-[var(--text-secondary)] mt-1 max-w-xs whitespace-normal">{row.note}</div> : null}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                      <div className="p-3 border-t border-[var(--border-subtle)] flex items-center justify-between gap-2">
                        <button className="px-3 py-1.5 rounded border border-[var(--border-subtle)] disabled:opacity-40" disabled={historyOffset<=0 || historyLoading} onClick={() => setHistoryOffset(Math.max(0,historyOffset-HISTORY_PAGE_SIZE))}>صفحهٔ قبل</button>
                        <span className="text-[10px] text-[var(--text-secondary)]">{toFa(Math.min(historyOffset+1,historyData?.total||0))} تا {toFa(Math.min(historyOffset+HISTORY_PAGE_SIZE,historyData?.total||0))}</span>
                        <button className="px-3 py-1.5 rounded border border-[var(--border-subtle)] disabled:opacity-40" disabled={historyOffset+HISTORY_PAGE_SIZE >= (historyData?.total||0) || historyLoading} onClick={() => setHistoryOffset(historyOffset+HISTORY_PAGE_SIZE)}>صفحهٔ بعد</button>
                      </div>
                    </>
                  )}
                </section>
              )}

              <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
                <h3 className="font-bold mb-1">ارزش تقریبی موقعیت‌های افشاشده در هر تاریخ ({currency === 'IRT' ? 'همت' : 'همر'})</h3>
                <p className="text-[10px] text-[var(--text-secondary)] mb-2">این نمودار فقط نمادهایی را جمع می‌کند که در همان تاریخ تعداد سهام و قیمت معتبر داشته‌اند؛ موجودی قبلی به روزهای بدون افشا منتقل نمی‌شود.</p>
                <LWChart data={chartSeries} height={220} theme={theme} color="#2962ff" unitLabel={currency === 'IRT' ? 'همت' : 'همر'} dateMode={dateMode} />
              </div>

              <div className="rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] overflow-x-auto">
                <div className="p-2.5 border-b border-[var(--border-subtle)] font-bold">آخرین رکوردهای مستقیم این شناسه (نه لزوماً مالکیت امروز)</div>
                <table className="w-full text-right">
                  <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                    <tr><th className="p-2">نماد</th><th className="p-2">تاریخ مشاهده</th><th className="p-2">تاریخ قیمت</th><th className="p-2">تعداد سهام</th><th className="p-2">درصد مالکیت</th><th className="p-2">قیمت</th><th className="p-2">ارزش</th></tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)]">
                    {portfolio.confirmed_positions.length === 0 ? (
                      <tr><td colSpan="7" className="p-4 text-center text-[var(--text-secondary)]">هیچ پوزیشن مستقیمی برای این شناسه ثبت نشده است.</td></tr>
                    ) : portfolio.confirmed_positions.map(p => (
                      <tr key={`${p.symbol}-${p.id_scheme}-${p.shareholder_id}`}>
                        <td className="p-2 font-bold text-blue-400 cursor-pointer" onClick={() => onOpenStock(p.symbol)}>{p.symbol}</td>
                        <td className="p-2 num">{fmtDate(p.observation_date_jalali)}</td>
                        <td className="p-2 num">{p.price_date_jalali ? fmtDate(p.price_date_jalali) : '—'}</td>
                        <td className="p-2 num">{fmtNum(p.shares)}</td>
                        <td className="p-2 num">{fmtPct(p.ownership_percent_calculated)}</td>
                        <td className="p-2 num">{p.price ? fmtNum(currency === 'IRT' ? p.price / 10 : p.price) : '—'}</td>
                        <td className="p-2 num font-semibold">{p.holding_value_rial !== null ? fmtMoney(p.holding_value_rial, currency) : 'بدون قیمت معتبر'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="rounded border border-blue-500/30 bg-blue-500/5 overflow-x-auto">
                <div className="p-2.5 border-b border-blue-500/30 font-bold text-blue-300">آخرین رکوردهای شناسه‌های محتمل (با عدم‌قطعیت هویت)</div>
                <table className="w-full text-right">
                  <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                    <tr><th className="p-2">نماد</th><th className="p-2">تاریخ مشاهده</th><th className="p-2">تاریخ قیمت</th><th className="p-2">تعداد سهام</th><th className="p-2">درصد مالکیت</th><th className="p-2">قیمت</th><th className="p-2">ارزش</th><th className="p-2">هویت</th></tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)]">
                    {(portfolio.probable_positions || []).length === 0 ? (
                      <tr><td colSpan="8" className="p-4 text-center text-[var(--text-secondary)]">پوزیشن «محتمل»ی برای این سرمایه‌گذار ثبت نشده است.</td></tr>
                    ) : portfolio.probable_positions.map(p => (
                      <tr key={`${p.symbol}-${p.id_scheme}-${p.shareholder_id}`}>
                        <td className="p-2 font-bold text-blue-400 cursor-pointer" onClick={() => onOpenStock(p.symbol)}>{p.symbol}</td>
                        <td className="p-2 num">{fmtDate(p.observation_date_jalali)}</td>
                        <td className="p-2 num">{fmtNum(p.shares)}</td>
                        <td className="p-2 num">{fmtPct(p.ownership_percent_calculated)}</td>
                        <td className="p-2 num">{p.price ? fmtNum(currency === 'IRT' ? p.price / 10 : p.price) : '—'}</td>
                        <td className="p-2 num font-semibold">{p.holding_value_rial !== null ? fmtMoney(p.holding_value_rial, currency) : 'بدون قیمت معتبر'}</td>
                        <td className="p-2"><IdentityBadge status={p.identity_status} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {portfolio.uncertain_candidates.length > 0 && (
                <div className="rounded border border-amber-500/40 bg-amber-500/5 p-3 space-y-1.5">
                  <div className="font-bold text-amber-300">رکوردهای سهام‌داری غیرقطعی (محاسبه‌نشده در جمع کل)</div>
                  {portfolio.uncertain_candidates.map((u, i) => (
                    <div key={i} className="p-2 rounded bg-[var(--bg-panel)] border border-[var(--border-subtle)] flex justify-between">
                      <span>نماد: <strong className="text-blue-400 cursor-pointer" onClick={() => onOpenStock(u.symbol)}>{u.symbol}</strong> ({u.id_scheme}: {u.shareholder_id})</span>
                      <span className="num">سهام: {fmtNum(u.shares)}</span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      );
    }

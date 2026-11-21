/* ============================================================================
 * MODULE 05 — 04-MARKET-DASHBOARD
 * Responsibility: Market overview page and its local data loading, filtering and presentation.
 * Load order: after 00-runtime.jsx and in the numeric order used by index HTML.
 * Shared contracts: React hooks and declarations from earlier modules are
 * available in this page. Keep API payloads and business rules unchanged here.
 * Maintenance: add focused comments beside non-obvious calculations, state
 * transitions, data provenance assumptions, and event handlers.
 * ========================================================================== */
// --- 4. Page 1: Multi-Stock Market Dashboard ---
    function MarketDashboardPage({ onOpenStock, onOpenInvestor, currency, dateMode }) {
      const [overview, setOverview] = useState(null);
      const [watch, setWatch] = useState({ total: 0, rows: [] });
      const [q, setQ] = useState('');
      const [market, setMarket] = useState('ALL');
      const [sortBy, setSortBy] = useState('disclosed_pct');
      const [sortDir, setSortDir] = useState('DESC');
      const [offset, setOffset] = useState(0);
      const [loading, setLoading] = useState(true);
      const [error, setError] = useState('');
      const limit = 50;

      useEffect(() => {
        apiFetch('/api/v1/market/overview').then(setOverview).catch(e => setError(e.message));
      }, []);

      useEffect(() => {
        const timer = setTimeout(() => {
          setLoading(true);
          const qs = new URLSearchParams({ q, market, sort_by: sortBy, sort_dir: sortDir, limit: String(limit), offset: String(offset) });
          apiFetch(`/api/v1/market/watch?${qs.toString()}`)
            .then(res => { setWatch(res); setError(''); })
            .catch(e => setError(e.message))
            .finally(() => setLoading(false));
        }, 200);
        return () => clearTimeout(timer);
      }, [q, market, sortBy, sortDir, offset]);

      const toggleSort = (col) => {
        setOffset(0);
        if (sortBy === col) setSortDir(d => d === 'DESC' ? 'ASC' : 'DESC');
        else { setSortBy(col); setSortDir('DESC'); }
      };

      const sum = overview?.summary || {};

      return (
        <div className="space-y-3">
          <ErrorBox error={error} />

          <div className="grid grid-cols-1 md:grid-cols-4 gap-2.5">
            <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
              <div className="text-[11px] text-[var(--text-secondary)]">شرکت‌های واردشده در پایگاه داده</div>
              <div className="text-lg font-bold num mt-1 text-blue-400">{fmtNum(sum.total_companies ?? 0)} نماد</div>
              <div className="text-[11px] text-[var(--text-secondary)] mt-1">
                بورس: <span className="num">{fmtNum(sum.tse_companies ?? 0)}</span> | فرابورس: <span className="num">{fmtNum(sum.ifb_companies ?? 0)}</span> | نامشخص: <span className="num">{fmtNum(sum.unknown_market_companies ?? 0)}</span>
              </div>
            </div>

            <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
              <div className="text-[11px] text-[var(--text-secondary)]">پوشش زمانی و حجم رکوردها</div>
              <div className="text-sm font-bold num mt-1">
                از {sum.earliest_date_jalali ? fmtDate(sum.earliest_date_jalali) : '—'} تا {sum.latest_date_jalali ? fmtDate(sum.latest_date_jalali) : '—'}
              </div>
              <div className="text-[11px] text-[var(--text-secondary)] mt-1">کل رکوردهای سهام‌داری: <span className="num">{fmtNum(sum.total_holdings_rows ?? 0)}</span></div>
            </div>

            <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
              <div className="flex justify-between items-center text-[11px] text-[var(--text-secondary)]">
                <span>شاخص‌های بازار (TSE / IFB)</span>
                <span className="px-1.5 py-0.5 rounded bg-slate-700/60 text-slate-300">داده موجود نیست</span>
              </div>
              <div className="mt-2 text-xs text-[var(--text-muted)]">
                شاخص کل و حجم معاملات در داده‌های جمع‌آوری‌شده موجود نیست (در انتظار اتصال فید بازار).
              </div>
            </div>

            <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
              <div className="text-[11px] text-[var(--text-secondary)]">آخرین به‌روزرسانی دیتابیس</div>
              <div className="text-sm font-bold num mt-1">{sum.last_updated_at ? fmtDateTime(sum.last_updated_at) : '—'}</div>
              <div className="text-[11px] text-emerald-400 mt-1">منبع: SQLite محلی</div>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-12 gap-3">
            <div className="lg:col-span-8 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] overflow-hidden">
              <div className="p-2.5 border-b border-[var(--border-subtle)] flex flex-wrap justify-between items-center gap-2">
                <h2 className="text-xs font-bold">فهرست کل نمادهای واردشده ({fmtNum(watch.total)} نماد)</h2>
                <div className="flex flex-wrap gap-2 text-xs">
                  <input value={q} onChange={e => { setQ(e.target.value); setOffset(0); }} placeholder="جستجوی نماد یا شرکت..." className="px-2.5 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] w-48" />
                  <select value={market} onChange={e => { setMarket(e.target.value); setOffset(0); }} className="px-2 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)]">
                    <option value="ALL">همه بازارها</option>
                    <option value="TSE">بورس (TSE)</option>
                    <option value="IFB">فرابورس (IFB)</option>
                    <option value="UNKNOWN">تعیین‌نشده (UNKNOWN)</option>
                  </select>
                </div>
              </div>

              <div className="overflow-x-auto">
                <table className="w-full text-xs text-right">
                  <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)] border-b border-[var(--border-subtle)]">
                    <tr>
                      <th className="p-2 cursor-pointer" onClick={() => toggleSort('symbol')}>نماد ⇅</th>
                      <th className="p-2">نام شرکت</th>
                      <th className="p-2">بازار</th>
                      <th className="p-2 cursor-pointer" onClick={() => toggleSort('total_shares')}>کل سهام ⇅</th>
                      <th className="p-2 cursor-pointer" onClick={() => toggleSort('market_value')}>ارزش بازار ⇅</th>
                      <th className="p-2 cursor-pointer" onClick={() => toggleSort('disclosed_count')}>سهام‌دار عمده ⇅</th>
                      <th className="p-2 cursor-pointer" onClick={() => toggleSort('disclosed_pct')}>مالکیت عمده٪ ⇅</th>
                      <th className="p-2 cursor-pointer" onClick={() => toggleSort('latest_date')}>آخرین تاریخ ⇅</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)]">
                    {loading ? (
                      <tr><td colSpan="8" className="p-6 text-center text-[var(--text-secondary)]">در حال بارگذاری...</td></tr>
                    ) : watch.rows.length === 0 ? (
                      <tr><td colSpan="8" className="p-6 text-center text-[var(--text-secondary)]">نمادی یافت نشد. ابتدا پایپلاین ایمپورت را اجرا کنید.</td></tr>
                    ) : watch.rows.map(r => (
                      <tr key={r.symbol} onClick={() => onOpenStock(r.symbol)} className="hover:bg-[var(--bg-elevated)] cursor-pointer">
                        <td className="p-2 font-bold text-blue-400">{r.symbol}</td>
                        <td className="p-2">{r.company_name || '—'}</td>
                        <td className="p-2"><span className="px-1.5 py-0.5 rounded bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[10px]">{r.market}</span></td>
                        <td className="p-2 num">{fmtNum(r.total_company_shares)}</td>
                        <td className="p-2 num" title={r.latest_price ? `آخرین قیمت: ${fmtNum(r.latest_price)} ریال (${fmtGregorianDate(r.latest_price_date)})` : 'قیمت موجود نیست'}>{r.market_value !== null && r.market_value !== undefined ? fmtMoney(r.market_value, currency) : '—'}</td>
                        <td className="p-2 num" title={r.raw_ownership_percent_sum !== null && r.raw_ownership_percent_sum !== undefined && r.deduped_ownership_percent_sum !== null && Math.abs(r.raw_ownership_percent_sum - r.deduped_ownership_percent_sum) > 0.001 ? `تعداد خام پیش از یک‌تا‌سازی: ${toFa(r.disclosed_shareholder_count)}` : undefined}>{toFa(r.deduped_shareholder_count ?? r.disclosed_shareholder_count)}</td>
                        <td className="p-2 num font-semibold" title={r.raw_ownership_percent_sum !== null && r.raw_ownership_percent_sum !== undefined && r.deduped_ownership_percent_sum !== null && Math.abs(r.raw_ownership_percent_sum - r.deduped_ownership_percent_sum) > 0.001 ? `جمع خام پیش از یک‌تا‌سازی: ${fmtPct(r.raw_ownership_percent_sum)} (پیش از حذف نمای تکراری منبع و یکسان‌سازی شناسه‌های صریح)` : undefined}>{r.deduped_ownership_percent_sum === null || r.deduped_ownership_percent_sum === undefined ? 'نامشخص' : (Number(r.deduped_ownership_percent_sum) > 100.000001 || Number(r.deduped_ownership_percent_sum) < 0) ? <span className="text-amber-400" title="مجموع مالکیت پس از حذف موارد اثبات‌شدهٔ تکرار و محاسبه با مخرج همان تاریخ هنوز ناسازگار است؛ برای تحلیل معتبر نیست.">نیازمند بررسی</span> : fmtPct(r.deduped_ownership_percent_sum)}</td>
                        <td className="p-2 num text-[var(--text-secondary)]">{fmtDate(r.latest_date_jalali)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="p-2 border-t border-[var(--border-subtle)] flex justify-between items-center text-xs">
                <span className="text-[var(--text-secondary)]">نمایش {fmtNum(watch.total === 0 ? 0 : offset + 1)} تا {fmtNum(Math.min(offset + limit, watch.total))} از {fmtNum(watch.total)}</span>
                <div className="flex gap-2">
                  <button disabled={offset === 0} onClick={() => setOffset(o => Math.max(0, o - limit))} className="px-2.5 py-1 rounded border border-[var(--border-subtle)] disabled:opacity-40">صفحه قبل</button>
                  <button disabled={offset + limit >= watch.total} onClick={() => setOffset(o => o + limit)} className="px-2.5 py-1 rounded border border-[var(--border-subtle)] disabled:opacity-40">صفحه بعد</button>
                </div>
              </div>
            </div>

            <div className="lg:col-span-4 space-y-2.5">
              <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
                <h3 className="text-xs font-bold mb-2">بزرگ‌ترین تغییرات دیروز {overview?.top_changes_date_jalali ? fmtDate(overview.top_changes_date_jalali) : ''}</h3>
                <div className="space-y-1.5 text-xs">
                  {(overview?.top_ownership_changes || []).length === 0 ? (
                    <div className="text-[var(--text-secondary)]">تغییری ثبت نشده است.</div>
                  ) : overview.top_ownership_changes.map((m, i) => (
                    <div key={i} onClick={() => onOpenStock(m.symbol)} className="p-2 rounded bg-[var(--bg-elevated)] border border-[var(--border-subtle)] cursor-pointer hover:border-blue-500">
                      <div className="flex justify-between font-semibold">
                        <span className="text-blue-400">{m.symbol}</span>
                        <span className={`num ${m.shares_change > 0 ? 'text-[var(--up)]' : 'text-[var(--down)]'}`}>
                          {m.shares_change > 0 ? '+' : ''}{fmtNum(m.shares_change)}
                        </span>
                      </div>
                      <div className="text-[11px] text-[var(--text-secondary)] truncate">{m.shareholder_name}</div>
                      {m.value_rial !== null && m.value_rial !== undefined && (
                        <div className="text-[11px] num text-[var(--text-secondary)]">ارزش تغییر: {fmtMoney(m.value_rial, currency)}</div>
                      )}
                    </div>
                  ))}
                </div>
              </div>

              <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
                <h3 className="text-xs font-bold mb-2">سهام‌داران عمده تازه‌مشاهده‌شده</h3>
                <div className="space-y-1.5 text-xs">
                  {(overview?.recently_observed_shareholders || []).map((n, i) => (
                    <div key={i} onClick={() => (n.id_scheme && n.shareholder_id && onOpenInvestor) ? onOpenInvestor(`${n.id_scheme}:${n.shareholder_id}`) : onOpenStock(n.symbol)} className="p-2 rounded bg-[var(--bg-elevated)] border border-[var(--border-subtle)] cursor-pointer" title="کلیک: پرتفوی این سهام‌دار">
                      <div className="flex justify-between">
                        <span className="font-bold text-blue-400" onClick={(e) => { e.stopPropagation(); onOpenStock(n.symbol); }}>{n.symbol}</span>
                        <span className="text-[10px] text-amber-400">اولین مشاهده در داده</span>
                      </div>
                      <div className="text-[11px] truncate">{n.shareholder_name} ({fmtPct(n.ownership_percent_calculated)})</div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>
      );
    }

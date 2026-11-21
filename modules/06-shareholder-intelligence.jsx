/* ============================================================================
 * MODULE 07 — 06-SHAREHOLDER-INTELLIGENCE
 * Responsibility: Cross-company shareholder intelligence and identity-linked investor views.
 * Load order: after 00-runtime.jsx and in the numeric order used by index HTML.
 * Shared contracts: React hooks and declarations from earlier modules are
 * available in this page. Keep API payloads and business rules unchanged here.
 * Maintenance: add focused comments beside non-obvious calculations, state
 * transitions, data provenance assumptions, and event handlers.
 * ========================================================================== */
// --- 6. Page 3: Cross-Company Shareholder Intelligence ---
    // --- 6. Page 3: Cross-Company Shareholder Intelligence ---
    // One row per PERSON (grouped by the pipeline's own identity links),
    // not per (holder, symbol). Clicking a holder shows their whole
    // portfolio: first the combined value chart (sum of all symbols),
    // then one chart per company; clicking a company swaps the main
    // chart to that company's series.
    function ShareholderIntelligencePage({ currency, theme, onOpenStock, onOpenInvestor, dateMode }) {
      const [q, setQ] = useState('');
      const [symbolFilter, setSymbolFilter] = useState('');
      const [typeFilter, setTypeFilter] = useState('ALL');
      const [minPct, setMinPct] = useState('');
      const [multiOnly, setMultiOnly] = useState(false);
      const [rows, setRows] = useState([]);
      const [selected, setSelected] = useState(null);
      const [portfolio, setPortfolio] = useState(null);
      const [histories, setHistories] = useState({});
      const [mainChart, setMainChart] = useState('combined');
      const [loading, setLoading] = useState(false);
      const [offset, setOffset] = useState(0);

      useEffect(() => {
        const timer = setTimeout(() => {
          setLoading(true);
          const qs = new URLSearchParams({
            q, symbol: symbolFilter, shareholder_type: typeFilter,
            multi_company_only: String(multiOnly), limit: '40', offset: String(offset),
          });
          if (minPct !== '') qs.set('min_ownership', minPct);
          apiFetch(`/api/v1/shareholders/search?${qs.toString()}`)
            .then(res => setRows(res.rows || []))
            .finally(() => setLoading(false));
        }, 250);
        return () => clearTimeout(timer);
      }, [q, symbolFilter, typeFilter, minPct, multiOnly, offset]);

      const loadGroup = (group) => {
        setSelected(group);
        setPortfolio(null);
        setHistories({});
        setMainChart('combined');
        apiFetch(`/api/v1/portfolios/${encodeURIComponent(group.group_id)}`)
          .then(res => setPortfolio(res))
          .catch(() => {});
        // One % history per company, using that symbol's own (scheme, id).
        (group.symbols || []).forEach(s => {
          const qs = new URLSearchParams({
            symbol: s.symbol, id_scheme: s.id_scheme,
            shareholder_id: String(s.shareholder_id),
          });
          apiFetch(`/api/v1/shareholders/observation-history?${qs.toString()}`)
            .then(res => setHistories(prev => ({ ...prev, [s.symbol]: res.history || [] })))
            .catch(() => {});
        });
      };

      const combinedSeries = useMemo(() => {
        // Values in همر (1e12 Rial) in IRR mode, همت (1e12 Toman = 1e13 Rial)
        // in IRT mode -- same divisor as the portfolio page.
        const divisor = currency === 'IRT' ? 1e13 : 1e12;
        return (portfolio?.history_series || []).map(p => ({
          time: p.time,
          value: (p.combined_value_rial || 0) / divisor,
        }));
      }, [portfolio, currency]);

      const valuedPositions = portfolio
        ? [...(portfolio.confirmed_positions || []), ...(portfolio.probable_positions || [])]
        : [];
      const totalValue = portfolio
        ? ((portfolio.summary?.confirmed_combined_value_rial || 0)
           + (portfolio.summary?.probable_combined_value_rial || 0))
        : null;
      const totalShares = valuedPositions.reduce((a, p) => a + (p.shares || 0), 0);

      return (
        <div className="space-y-3 text-xs">
          <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] flex flex-wrap justify-between items-center gap-2">
            <div>
              <h1 className="text-sm font-bold">پایگاه جامع سهام‌داران در تمام شرکت‌های واردشده</h1>
              <p className="text-[11px] text-[var(--text-secondary)]">هر سطر یک سهام‌دار است — با همهٔ شرکت‌هایی که در آن‌ها سهام دارد. جستجو بر اساس نام، شناسه TSETMC، نماد، حداقل درصد مالکیت و حضور هم‌زمان در چند شرکت</p>
            </div>
            <div className="flex flex-wrap gap-2 items-center">
              <input value={q} onChange={e => { setQ(e.target.value); setOffset(0); }} placeholder="نام سهام‌دار یا شناسه..." className="px-2.5 py-1.5 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] w-52" />
              <input value={symbolFilter} onChange={e => { setSymbolFilter(e.target.value); setOffset(0); }} placeholder="نماد..." className="px-2.5 py-1.5 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] w-24" />
              <input type="number" value={minPct} onChange={e => { setMinPct(e.target.value); setOffset(0); }} placeholder="حداقل ٪" className="px-2 py-1.5 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] w-24 num" />
              <select value={typeFilter} onChange={e => { setTypeFilter(e.target.value); setOffset(0); }} className="px-2 py-1.5 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)]">
                <option value="ALL">همه</option>
                <option value="individual">حقیقی</option>
                <option value="legal">حقوقی</option>
              </select>
              <label className="flex items-center gap-1 cursor-pointer">
                <input type="checkbox" checked={multiOnly} onChange={e => { setMultiOnly(e.target.checked); setOffset(0); }} />
                <span>فقط سهام‌داران چندشرکتی</span>
              </label>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-12 gap-3">
            <div className="lg:col-span-7 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] overflow-x-auto">
              <table className="w-full text-right">
                <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                  <tr><th className="p-2">نام سهام‌دار</th><th className="p-2">نمادها</th><th className="p-2">شناسه</th><th className="p-2">بیشترین مالکیت٪</th><th className="p-2">آخرین تغییر</th></tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-subtle)]">
                  {loading ? (
                    <tr><td colSpan="5" className="p-6 text-center text-[var(--text-secondary)]">در حال جستجو...</td></tr>
                  ) : rows.map((r, i) => (
                    <tr key={r.group_id} onClick={() => loadGroup(r)} className="cursor-pointer hover:bg-[var(--bg-elevated)]">
                      <td className="p-2 font-semibold">{r.shareholder_name}</td>
                      <td className="p-2">
                        <span className="font-bold text-blue-400">{toFa(r.n_companies)} شرکت</span>
                        <span className="text-[10px] text-[var(--text-secondary)] block">{(r.symbols || []).slice(0, 3).map(s => s.symbol).join('، ')}{(r.symbols || []).length > 3 ? '…' : ''}</span>
                      </td>
                      <td className="p-2 num text-[10px]">{r.shareholder_id}</td>
                      <td className="p-2 num font-semibold">{fmtPct(r.latest_ownership_pct)}</td>
                      <td className={`p-2 num ${r.latest_shares_change > 0 ? 'text-[var(--up)]' : r.latest_shares_change < 0 ? 'text-[var(--down)]' : ''}`}>{fmtNum(r.latest_shares_change)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="p-2 border-t border-[var(--border-subtle)] flex justify-between">
                <button disabled={offset === 0} onClick={() => setOffset(o => Math.max(0, o - 40))} className="px-2 py-1 rounded border border-[var(--border-subtle)] disabled:opacity-40">صفحه قبل</button>
                <button disabled={rows.length < 40} onClick={() => setOffset(o => o + 40)} className="px-2 py-1 rounded border border-[var(--border-subtle)] disabled:opacity-40">صفحه بعد</button>
              </div>
            </div>

            <div className="lg:col-span-5 p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-2.5 lg:sticky lg:top-24 self-start">
              {selected ? (
                <>
                  <div className="flex justify-between items-start gap-2">
                    <div>
                      <div className="font-bold text-sm">{selected.shareholder_name}</div>
                      <div className="text-[11px] text-[var(--text-secondary)]">
                        {selected.shareholder_type === 'legal' ? 'حقوقی' : selected.shareholder_type === 'individual' ? 'حقیقی' : ''} | بازه مشاهده: <span className="num">{fmtDate(selected.first_observed_date_jalali)} تا {fmtDate(selected.last_observed_date_jalali)}</span>
                      </div>
                      <div className="mt-1">
                        ارزش سرمایه‌گذاری: <span className="font-bold num">{totalValue !== null ? fmtMoney(totalValue, currency) : '…'}</span>
                        {' '} | تعداد سهام: <span className="font-bold num">{fmtNum(totalShares)}</span>
                        {' '} | شرکت‌ها: <span className="font-bold num">{toFa(selected.n_companies)}</span>
                      </div>
                    </div>
                    <button onClick={() => onOpenInvestor && onOpenInvestor(selected.group_id)} className="px-2 py-1 rounded border border-blue-500/40 text-blue-400 shrink-0">پرتفوی کامل در تب ۴</button>
                  </div>
                  <div>
                    <div className="font-bold mb-1 flex justify-between items-center">
                      <span>{mainChart === 'combined' ? `مجموع ارزش همهٔ نمادها (${currency === 'IRT' ? 'همت' : 'همر'})` : `روند مالکیت — ${mainChart}`}</span>
                      {mainChart !== 'combined' && (
                        <button onClick={() => setMainChart('combined')} className="px-2 py-0.5 rounded border border-[var(--border-subtle)] text-[11px]">بازگشت به مجموع</button>
                      )}
                    </div>
                    <LWChart data={mainChart === 'combined' ? combinedSeries : (histories[mainChart] || [])} height={220} theme={theme} color="#00c853" unitLabel={mainChart === 'combined' ? (currency === 'IRT' ? 'همت' : 'همر') : '٪'} dateMode={dateMode} />
                  </div>
                  {(selected.symbols || []).length > 0 && (
                    <div className="space-y-2">
                      <div className="font-bold">چارت هر شرکت</div>
                      {selected.symbols.map(s => (
                        <div key={s.symbol} onClick={() => setMainChart(s.symbol)} className={`p-2 rounded border cursor-pointer ${mainChart === s.symbol ? 'border-blue-500/60 bg-blue-500/5' : 'border-[var(--border-subtle)]'}`}>
                          <div className="flex justify-between items-center mb-1">
                            <button onClick={(e) => { e.stopPropagation(); onOpenStock(s.symbol); }} className="font-bold text-blue-400 underline">{s.symbol}</button>
                            <span className="num text-[var(--text-secondary)]">{fmtPct(s.latest_ownership_pct)} | {fmtNum(s.shares)} سهم</span>
                          </div>
                          <LWChart data={histories[s.symbol] || []} height={120} theme={theme} color="#00c853" unitLabel="٪" dateMode={dateMode} />
                        </div>
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <div className="p-8 text-center text-[var(--text-secondary)]">یک سطر را برای مشاهده پرتفوی کامل سهام‌دار انتخاب کنید.</div>
              )}
            </div>
          </div>
        </div>
      );
    }

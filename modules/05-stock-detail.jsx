/* ============================================================================
 * MODULE 06 — 05-STOCK-DETAIL
 * Responsibility: Single-stock detail page, price history, events and shareholder views.
 * Load order: after 00-runtime.jsx and in the numeric order used by index HTML.
 * Shared contracts: React hooks and declarations from earlier modules are
 * available in this page. Keep API payloads and business rules unchanged here.
 * Maintenance: add focused comments beside non-obvious calculations, state
 * transitions, data provenance assumptions, and event handlers.
 * ========================================================================== */
// --- 5. Page 2: Dynamic Stock Detail Page ---
    function StockDetailPage({ symbol, currency, theme, dateMode, onOpenInvestor }) {
      const [detail, setDetail] = useState(null);
      const [holdersRes, setHoldersRes] = useState({ total: 0, rows: [], date_jalali: null });
      const [selectedDate, setSelectedDate] = useState('');
      const [seriesRes, setSeriesRes] = useState({ company_series: [], company_shares_series: [], holder_series: [], events: [] });
      const [selectedHolder, setSelectedHolder] = useState('');
      const [actions, setActions] = useState([]);
      const [quality, setQuality] = useState(null);
      const [prices, setPrices] = useState([]);
      const [chartType, setChartType] = useState('candlestick');
      const [chartMetric, setChartMetric] = useState('pct');
      const [tab, setTab] = useState('overview');
      const [sortBy, setSortBy] = useState('shares');
      const [loading, setLoading] = useState(false);
      const [error, setError] = useState('');

      useEffect(() => {
        if (!symbol) return;
        setLoading(true);
        setSelectedDate('');
        setSelectedHolder('');
        Promise.all([
          apiFetch(`/api/v1/stocks/${encodeURIComponent(symbol)}`),
          apiFetch(`/api/v1/stocks/${encodeURIComponent(symbol)}/ownership-series`),
          apiFetch(`/api/v1/stocks/${encodeURIComponent(symbol)}/corporate-actions`),
          apiFetch(`/api/v1/stocks/${encodeURIComponent(symbol)}/data-quality`),
          apiFetch(`/api/v1/stocks/${encodeURIComponent(symbol)}/prices`).catch(() => ({ prices: [] })),
        ])
          .then(([d, ser, ca, dq, pr]) => {
            setDetail(d);
            setSeriesRes(ser);
            setActions(ca.corporate_actions || []);
            setQuality(dq);
            setPrices(pr.prices || []);
            setError('');
          })
          .catch(e => setError(e.message))
          .finally(() => setLoading(false));
      }, [symbol]);

      useEffect(() => {
        if (!symbol) return;
        const qs = new URLSearchParams({ sort_by: sortBy, sort_dir: 'DESC', limit: '100' });
        if (selectedDate) qs.set('date_jalali', selectedDate);
        apiFetch(`/api/v1/stocks/${encodeURIComponent(symbol)}/shareholders?${qs.toString()}`)
          .then(setHoldersRes)
          .catch(() => {});
      }, [symbol, selectedDate, sortBy]);

      useEffect(() => {
        if (!symbol) return;
        const qs = new URLSearchParams();
        if (selectedHolder) {
          const [scheme, shId] = selectedHolder.split('|');
          qs.set('id_scheme', scheme);
          qs.set('shareholder_id', shId);
        }
        apiFetch(`/api/v1/stocks/${encodeURIComponent(symbol)}/ownership-series?${qs.toString()}`)
          .then(setSeriesRes)
          .catch(() => {});
      }, [symbol, selectedHolder]);

      // One marker per DATE (not per event): hovering a marker opens a
      // small popup with every event of that day. Marker text is EMPTY:
      // texts on dense daily markers overlapped into an unreadable pile.
      // Only REAL share changes are events here (increase/decrease):
      // a first observation ('new') or an unchanged row is not a buy/sell.
      const eventsByDate = useMemo(() => {
        const byDate = new Map();
        for (const ev of (seriesRes.events || [])) {
          if (!ev.date_gregorian) continue;
          if (ev.change_type !== 'increase' && ev.change_type !== 'decrease') continue;
          const d = String(ev.date_gregorian).slice(0, 10);
          if (!byDate.has(d)) byDate.set(d, { events: [], capitalIncreases: [] });
          byDate.get(d).events.push(ev);
        }
        for (const ca of actions) {
          if (!ca.event_date_gregorian) continue;
          if (ca.action_type !== 'capital_increase') continue;
          const d = String(ca.event_date_gregorian).slice(0, 10);
          if (!byDate.has(d)) byDate.set(d, { events: [], capitalIncreases: [] });
          byDate.get(d).capitalIncreases.push(ca);
        }
        return byDate;
      }, [seriesRes.events, actions]);
      const [hoveredDay, setHoveredDay] = useState(null);
      const [hoveredPriceDay, setHoveredPriceDay] = useState(null);
      // Toggle to hide the event circles / capital-increase triangles.
      const [showMarkers, setShowMarkers] = useState(true);
      const eventMarkers = useMemo(() => {
        const out = [];
        for (const [d, day] of eventsByDate) {
          if (day.events.length === 0) continue;
          const net = day.events.reduce((s, ev) => s + (ev.shares_change || 0), 0);
          out.push({
            time: d,
            position: net >= 0 ? 'aboveBar' : 'belowBar',
            color: net >= 0 ? '#00c853' : '#ff3d00',
            shape: 'circle',
            text: '',
          });
        }
        for (const ca of actions) {
          if (!ca.event_date_gregorian) continue;
          if (ca.action_type !== 'capital_increase') continue;
          out.push({
            time: ca.event_date_gregorian,
            position: 'aboveBar',
            color: '#f59e0b',
            shape: 'arrowUp',
            text: '',
          });
        }
        return out.sort((a, b) => a.time.localeCompare(b.time));
      }, [eventsByDate, actions, currency]);
      const priceCapMarkers = useMemo(() => actions
        .filter(ca => ca.event_date_gregorian && ca.action_type === 'capital_increase')
        .map(ca => ({
          time: ca.event_date_gregorian,
          position: 'aboveBar',
          color: '#f59e0b',
          shape: 'arrowUp',
          text: '',
        })), [actions]);
      const priceCloseSeries = useMemo(() => prices.map(p => ({ time: p.time, value: p.close })), [prices]);
      // Holders, name-deduped: the same holder under several id schemes
      // counts once (best identity tier wins); the merged count is shown.
      const dedupedHolders = useMemo(() => dedupHolders(holdersRes.rows), [holdersRes.rows]);
      const unquantifiedNames = useMemo(() => unquantifiedHolders(holdersRes.rows), [holdersRes.rows]);

      if (!symbol) return <div className="p-8 text-center text-xs text-[var(--text-secondary)]">هنوز نمادی انتخاب یا ایمپورت نشده است. از داشبورد یا جستجوی بالا یک نماد انتخاب کنید.</div>;
      if (loading) return <div className="p-8 text-center text-xs text-[var(--text-secondary)]">در حال واکشی اطلاعات نماد {symbol}...</div>;
      if (error) return <ErrorBox error={error} />;
      if (!detail) return null;

      const inst = detail.instrument;
      const snap = detail.latest_snapshot || {};
      // The "چارت": rises when the major holders buy, falls when they sell.
      // Percent by default; shares shows the deduped total share count.
      // A selected holder shows only that holder's percent series.
      const chartData = selectedHolder
        ? seriesRes.holder_series
        : (chartMetric === 'shares' ? seriesRes.company_shares_series : seriesRes.company_series);
      const chartUnit = selectedHolder || chartMetric === 'pct' ? '٪' : 'سهم';
      const CHANGE_TYPE_FA = {
        new: 'جدید',
        increase: 'افزایش',
        decrease: 'کاهش',
        unchanged: 'بدون تغییر',
        unknown: 'نامشخص',
      };
      const ACTION_TYPE_FA = {
        capital_increase: 'افزایش سرمایه',
        price_adjustment: 'تعدیل قیمت',
      };
      const COMPLETENESS_FA = {
        complete_by_reported_percent: 'کامل (بر اساس درصد گزارش‌شده)',
        partial_major_holders_only: 'فقط سهام‌داران عمده',
        inconsistent_sum_over_100_percent: 'جمع مالکیت بیش از ۱۰۰٪ (داده تکراری)',
        no_holder_observations: 'بدون مشاهده سهام‌دار',
        holders_only_total_shares_unavailable: 'بدون کل سهام شرکت',
      };
      const mergedHoldersCount = holdersRes.rows.length - dedupedHolders.length;

      return (
        <div className="space-y-3">
          <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] flex flex-wrap justify-between items-center gap-3">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-base font-bold text-blue-400">{inst.symbol}</h1>
                <span className="text-sm font-bold">{inst.company_name || 'نام شرکت ثبت نشده'}</span>
                <span className="px-2 py-0.5 text-[10px] rounded bg-blue-500/15 text-blue-400">{inst.market}</span>
                {snap.data_completeness && <span className="px-2 py-0.5 text-[10px] rounded bg-amber-500/15 text-amber-300" title={snap.data_completeness}>{COMPLETENESS_FA[snap.data_completeness] || snap.data_completeness}</span>}
              </div>
              <div className="text-[11px] text-[var(--text-secondary)] mt-1">
                ins_code: <span className="num">{inst.ins_code || '—'}</span> | ISIN: <span className="num">{inst.cisin || '—'}</span> | بازه داده: <span className="num">{fmtDate(inst.first_holding_date_jalali)} تا {fmtDate(inst.latest_holding_date_jalali)}</span>
              </div>
            </div>

            <div className="flex flex-wrap gap-5 text-xs">
              <div>
                <div className="text-[var(--text-secondary)]">کل سهام شرکت</div>
                <div className="font-bold num">{fmtNum(snap.total_company_shares ?? inst.current_issued_shares)}</div>
              </div>
              <div title={snap.latest_price ? `آخرین قیمت: ${fmtNum(snap.latest_price)} ریال (${fmtGregorianDate(snap.latest_price_date)})` : 'قیمت موجود نیست'}>
                <div className="text-[var(--text-secondary)]">ارزش بازار</div>
                <div className="font-bold num">{snap.market_value !== null && snap.market_value !== undefined ? fmtMoney(snap.market_value, currency) : '—'}</div>
              </div>
              <div>
                <div className="text-[var(--text-secondary)]">سهام‌داران عمده افشاشده</div>
                <div className="font-bold num" title={snap.raw_disclosed_shareholder_count !== null && snap.raw_disclosed_shareholder_count !== undefined && snap.raw_disclosed_shareholder_count !== snap.disclosed_shareholder_count ? `تعداد خام پیش از یک‌تا‌سازی: ${toFa(snap.raw_disclosed_shareholder_count)}` : undefined}>{toFa(snap.disclosed_shareholder_count)}</div>
              </div>
              <div title={snap.total_shareholder_count_note || undefined}>
                <div className="text-[var(--text-secondary)]">کل سهام‌داران شرکت</div>
                <div className="font-bold text-amber-400">{snap.total_shareholder_count !== null && snap.total_shareholder_count !== undefined ? (snap.total_shareholder_count_note ? `≥ ${fmtNum(snap.total_shareholder_count)}` : fmtNum(snap.total_shareholder_count)) : '—'}</div>
                {snap.total_shareholder_count_note && <div className="text-[10px] text-amber-300">حداقل (ثبت نشده)</div>}
              </div>
              <div title={snap.raw_ownership_percent_sum !== null && snap.raw_ownership_percent_sum !== undefined && snap.disclosed_ownership_percent_sum !== null && Math.abs(snap.raw_ownership_percent_sum - snap.disclosed_ownership_percent_sum) > 0.001 ? `جمع خام پیش از یک‌تا‌سازی: ${fmtPct(snap.raw_ownership_percent_sum)} (پیش از حذف نمای تکراری منبع و یکسان‌سازی شناسه‌های صریح)` : undefined}>
                <div className="text-[var(--text-secondary)]">جمع مالکیت عمده</div>
                <div className="font-bold num">{(Number(snap.disclosed_ownership_percent_sum) > 100.000001 || Number(snap.disclosed_ownership_percent_sum) < 0) ? <span className="text-amber-400" title="جمع خام گزارش‌شده از محدودهٔ معتبر خارج است؛ تا زمان اصلاح داده، درصد نمایش داده نمی‌شود.">نیازمند بررسی</span> : fmtPct(snap.disclosed_ownership_percent_sum)}</div>
              </div>
              <div title="۱۰۰ منهای جمع مالکیت عمدهٔ افشاشده — سهامی که در بازار آزاد دست مردم است و قابل معامله است">
                <div className="text-[var(--text-secondary)]">سهام شناور</div>
                <div className="font-bold num text-emerald-400">{snap.disclosed_ownership_percent_sum !== null && snap.disclosed_ownership_percent_sum !== undefined && Number(snap.disclosed_ownership_percent_sum) >= 0 && Number(snap.disclosed_ownership_percent_sum) <= 100.000001 ? fmtPct(100 - snap.disclosed_ownership_percent_sum) : '—'}</div>
              </div>
            </div>
          </div>

          <div className="flex gap-1 border-b border-[var(--border-subtle)] text-xs overflow-x-auto">
            {[
              ['overview', 'نمودار و تاریخچه شرکت'],
              ['holders', `سهام‌داران عمده (${toFa(dedupedHolders.length)})`],
              ['events', `رویدادهای ورود/خروج و تغییر (${toFa(seriesRes.events?.length || 0)})`],
              ['actions', `افزایش سرمایه (${toFa(actions.filter(a => a.action_type === 'capital_increase').length)})`],
              ['quality', 'کیفیت داده و مغایرت‌ها'],
            ].map(([id, label]) => (
              <button key={id} onClick={() => setTab(id)} className={`px-3 py-2 border-b-2 whitespace-nowrap ${tab === id ? 'border-blue-500 text-blue-400 font-bold' : 'border-transparent text-[var(--text-secondary)]'}`}>
                {label}
              </button>
            ))}
          </div>

          {tab === 'overview' && (
            <div className="space-y-3">
              <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-2">
                <div className="flex flex-wrap justify-between items-center gap-2 text-xs">
                  <h2 className="font-bold">نمودار قیمت نماد</h2>
                  <div className="flex flex-wrap gap-1.5 items-center">
                    <span className="text-[var(--text-secondary)]">نوع نمودار:</span>
                    {[
                      ['candlestick', 'کندلی'],
                      ['area', 'ناحیه‌ای'],
                      ['line', 'خطی'],
                      ['bar', 'میله‌ای'],
                    ].map(([id, label]) => (
                      <button key={id} onClick={() => setChartType(id)} className={`px-2 py-0.5 rounded border border-[var(--border-subtle)] ${chartType === id ? 'bg-blue-600 text-white' : 'bg-[var(--bg-elevated)]'}`}>{label}</button>
                    ))}
                    <span className="text-[var(--text-secondary)]">({toFa(prices.length)} روز معاملاتی)</span>
                    <label className="flex items-center gap-1 cursor-pointer" title="نمایش/پنهان کردن دایره‌ها و مثلث‌های رویداد روی نمودار">
                      <input type="checkbox" checked={showMarkers} onChange={e => setShowMarkers(e.target.checked)} /> نمایش رویدادها
                    </label>
                  </div>
                </div>
                <div className="relative">
                  <LWChart data={priceCloseSeries} ohlc={prices} type={chartType} height={320} theme={theme} color="#2962ff" unitLabel="ریال" markers={showMarkers ? priceCapMarkers : []} dateMode={dateMode} enableDrawing={true} onHover={setHoveredPriceDay} />
                  {hoveredPriceDay && eventsByDate.get(hoveredPriceDay.time) && (
                    <div className="absolute top-2 right-2 z-10 w-80 max-w-[90%] max-h-64 overflow-y-auto p-2.5 rounded border border-amber-500/50 bg-[var(--bg-panel)] shadow-lg text-[11px] space-y-1.5">
                      <div className="flex justify-between items-center">
                        <span className="font-bold">رویدادهای {fmtGregorianDate(hoveredPriceDay.time)} ({toFa(eventsByDate.get(hoveredPriceDay.time).events.length)} رویداد)</span>
                        <button onClick={() => setHoveredPriceDay(null)} className="px-1.5 rounded border border-[var(--border-subtle)]">✕</button>
                      </div>
                      {eventsByDate.get(hoveredPriceDay.time).capitalIncreases.map((ca, i) => (
                        <div key={`ca-${i}`} className="text-amber-300">▲ افزایش سرمایه{ca.capital_increase_ratio ? ` ×${toFa(Number(ca.capital_increase_ratio).toFixed(2))}` : ''} — تعداد سهام زیاد می‌شود ولی درصد مالکیت ممکن است تغییر نکند (رقیق‌شدن)</div>
                      ))}
                      {eventsByDate.get(hoveredPriceDay.time).events.map((ev, i) => (
                        <div key={i} className="border-t border-[var(--border-subtle)] pt-1">
                          <div className="font-semibold">{ev.shareholder_name} {(ev.shares_change || 0) > 0 ? 'خرید' : 'فروش'}</div>
                          <div className="num">{fmtNum(Math.abs(ev.shares_change || 0))} سهم ({fmtPct(ev.ownership_percent_calculated)})</div>
                          <div className="num text-[var(--text-secondary)]">ارزش روز: {ev.value_rial !== null && ev.value_rial !== undefined ? fmtMoney(ev.value_rial, currency) : '—'}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
                <div className="text-[11px] text-[var(--text-secondary)]">▲ روی نمودار یعنی افزایش سرمایه — روی مثلث نگه دارید تا جزئیات آن روز را ببینید. با دکمه «تمام‌صفحه» نمودار بزرگ می‌شود و با ابزارهای ترسیم (خط روند، مستطیل، خط افقی) می‌توانید روی آن تحلیل کنید. دکمه‌های A (مقیاس خودکار) و L (لگاریتمی) پایین-راست نمودار هستند.</div>
              </div>

              <div className="grid grid-cols-1 lg:grid-cols-12 gap-3">
                <div className="lg:col-span-8 p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-2">
                  <div className="flex flex-wrap justify-between items-center gap-2 text-xs">
                    <h2 className="font-bold">چارت</h2>
                    <div className="flex flex-wrap gap-1.5 items-center">
                      <button onClick={() => setChartMetric('pct')} disabled={!!selectedHolder} className={`px-2 py-0.5 rounded border border-[var(--border-subtle)] ${chartMetric === 'pct' ? 'bg-blue-600 text-white' : 'bg-[var(--bg-elevated)]'} disabled:opacity-40`}>درصد مالکیت</button>
                      <button onClick={() => setChartMetric('shares')} disabled={!!selectedHolder} className={`px-2 py-0.5 rounded border border-[var(--border-subtle)] ${chartMetric === 'shares' ? 'bg-blue-600 text-white' : 'bg-[var(--bg-elevated)]'} disabled:opacity-40`}>تعداد سهام</button>
                      <label className="flex items-center gap-1 cursor-pointer" title="نمایش/پنهان کردن دایره‌ها و مثلث‌های رویداد روی نمودار">
                        <input type="checkbox" checked={showMarkers} onChange={e => setShowMarkers(e.target.checked)} /> نمایش رویدادها
                      </label>
                      <select value={selectedHolder} onChange={e => setSelectedHolder(e.target.value)} className="px-2 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] max-w-xs">
                        <option value="">جمع مالکیت عمده افشاشده شرکت</option>
                        {dedupedHolders.map(h => (
                          <option key={`${h.id_scheme}|${h.shareholder_id}`} value={`${h.id_scheme}|${h.shareholder_id}`}>
                            {h.shareholder_name} ({fmtPct(h.ownership_percent_calculated)})
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                  <div className="relative">
                    <LWChart data={chartData} height={270} theme={theme} color={selectedHolder ? '#00c853' : '#2962ff'} unitLabel={chartUnit} markers={showMarkers ? eventMarkers : []} dateMode={dateMode} onHover={setHoveredDay} />
                    {hoveredDay && eventsByDate.get(hoveredDay.time) && (
                      <div className="absolute top-2 right-2 z-10 w-80 max-w-[90%] max-h-64 overflow-y-auto p-2.5 rounded border border-blue-500/50 bg-[var(--bg-panel)] shadow-lg text-[11px] space-y-1.5">
                        <div className="flex justify-between items-center">
                          <span className="font-bold">رویدادهای {fmtGregorianDate(hoveredDay.time)} ({toFa(eventsByDate.get(hoveredDay.time).events.length)} رویداد)</span>
                          <button onClick={() => setHoveredDay(null)} className="px-1.5 rounded border border-[var(--border-subtle)]">✕</button>
                        </div>
                        {eventsByDate.get(hoveredDay.time).capitalIncreases.map((ca, i) => (
                          <div key={`ca-${i}`} className="text-amber-300">▲ افزایش سرمایه{ca.capital_increase_ratio ? ` ×${toFa(Number(ca.capital_increase_ratio).toFixed(2))}` : ''} — تعداد سهام زیاد می‌شود ولی درصد مالکیت ممکن است تغییر نکند (رقیق‌شدن)</div>
                        ))}
                        {eventsByDate.get(hoveredDay.time).events.map((ev, i) => (
                          <div key={i} className="border-t border-[var(--border-subtle)] pt-1">
                            <div className="font-semibold">{ev.shareholder_name} {(ev.shares_change || 0) > 0 ? 'خرید' : 'فروش'}</div>
                            <div className="num">{fmtNum(Math.abs(ev.shares_change || 0))} سهم ({fmtPct(ev.ownership_percent_calculated)})</div>
                            <div className="num text-[var(--text-secondary)]">ارزش روز: {ev.value_rial !== null && ev.value_rial !== undefined ? fmtMoney(ev.value_rial, currency) : '—'}</div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                  <div className="text-[11px] text-[var(--text-secondary)]">
                    نمودار تغییرات سهام‌داران: وقتی سهام‌داران می‌خرند بالا می‌رود و وقتی می‌فروشند پایین می‌آید (محور افقی: زمان، محور عمودی: {chartMetric === 'shares' && !selectedHolder ? 'تعداد سهام' : 'درصد مالکیت'}).
                    {selectedHolder ? ' برای سهام‌دار انتخاب‌شده فقط درصد نمایش داده می‌شود.' : ''}
                    {' '}● سبز/قرمز روی نمودار فقط روزهای خرید/فروش واقعی را نشان می‌دهد (اولین مشاهده یا روز بدون تغییر، رویداد ندارد) — روی نقطه نگه دارید تا تعداد و جزئیات رویدادهای آن روز را در پنجرهٔ کوچک ببینید؛ ▲ یعنی افزایش سرمایه (فقط افزایش سرمایهٔ واقعی، نه تعدیل قیمت) — روی مثلث هم نگه دارید تا جزئیاتش را ببینید. دکمه‌های A (مقیاس خودکار) و L (لگاریتمی) پایین-راست نمودار هستند.
                  </div>
                </div>

                <div className="lg:col-span-4 p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-2 text-xs">
                  <h3 className="font-bold">تاریخچه سرمایه و تمرکز</h3>
                  <div className="max-h-60 overflow-y-auto">
                    <table className="w-full text-[11px]">
                      <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                        <tr>
                          <th className="p-1.5 text-right">تاریخ</th>
                          <th className="p-1.5 text-right" title="تعداد سهام‌داران عمدهٔ افشاشده در آن روز، پس از یک‌تا‌سازی رکوردهای تکراری (همان سهام‌دار با چند شناسه یک بار شمرده شده است)">تعداد عمده ⓘ</th>
                          <th className="p-1.5 text-right" title="جمع درصد مالکیت همان سهام‌داران عمده در آن روز، پس از یک‌تا‌سازی؛ اگر همان سهام‌دار با چند شناسه تکرار شده باشد فقط یک بار شمرده می‌شود">درصد٪ ⓘ</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-[var(--border-subtle)]">
                        {detail.company_history.map(c => (
                          <tr key={c.date_jalali}>
                            <td className="p-1.5 num">{fmtDate(c.date_jalali)}</td>
                            <td className="p-1.5 num" title={c.raw_shareholder_count !== null && c.raw_shareholder_count !== undefined && c.raw_shareholder_count !== c.disclosed_shareholder_count ? `تعداد خام پیش از یک‌تا‌سازی: ${toFa(c.raw_shareholder_count)}` : 'تعداد سهام‌داران عمدهٔ یک‌تا‌شده'}>{toFa(c.disclosed_shareholder_count)}{c.raw_shareholder_count !== null && c.raw_shareholder_count !== undefined && c.raw_shareholder_count !== c.disclosed_shareholder_count ? <span className="text-[var(--text-secondary)]"> (خام: {toFa(c.raw_shareholder_count)})</span> : ''}</td>
                            <td className="p-1.5 num font-semibold" title={c.raw_ownership_percent_sum !== null && c.raw_ownership_percent_sum !== undefined && Math.abs(c.raw_ownership_percent_sum - c.disclosed_ownership_percent_sum) > 0.001 ? `جمع خام پیش از یک‌تا‌سازی: ${fmtPct(c.raw_ownership_percent_sum)}` : 'جمع درصد مالکیت سهام‌داران عمدهٔ یک‌تا‌شده'}>{(Number(c.disclosed_ownership_percent_sum) > 100.000001 || Number(c.disclosed_ownership_percent_sum) < 0) ? <span className="text-amber-400" title="مجموع مالکیت ناسازگار است؛ این تاریخ برای تحلیل معتبر نیست.">نیازمند بررسی</span> : fmtPct(c.disclosed_ownership_percent_sum)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="p-2 rounded bg-slate-800/50 border border-[var(--border-subtle)] text-[11px] text-[var(--text-secondary)]">
                    «تعداد عمده» یعنی چند سهام‌دار عمده در آن روز افشا شده‌اند و «درصد» یعنی مجموع مالکیت همان‌ها؛ هر دو پس از یک‌تا‌سازی رکوردهای تکراری محاسبه می‌شوند. با نگه داشتن نشانگر روی ستون‌ها توضیح کامل را ببینید.
                  </div>
                </div>
              </div>

              <div className="rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] overflow-x-auto">
                <div className="p-2.5 border-b border-[var(--border-subtle)] flex flex-wrap justify-between items-center gap-2 text-xs">
                  <span className="font-bold">سهام‌داران عمده ({toFa(dedupedHolders.length)} سهام‌دار یک‌تا‌شده از {toFa(holdersRes.rows.length)} رکورد)</span>
                  <span className="text-[var(--text-secondary)]">به ترتیب درصد مالکیت — برای جدول کامل به تب «سهام‌داران عمده» بروید</span>
                </div>
                <table className="w-full text-xs text-right">
                  <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                    <tr>
                      <th className="p-2">نام سهام‌دار</th>
                      <th className="p-2">نوع</th>
                      <th className="p-2">تعداد سهام</th>
                      <th className="p-2">درصد مالکیت</th>
                      <th className="p-2">تغییر سهام</th>
                      <th className="p-2">ارزش</th>
                      <th className="p-2">هویت</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)]">
                    {dedupedHolders.slice(0, 15).map(h => (
                      <tr key={`${h.id_scheme}-${h.shareholder_id}`} onClick={() => onOpenInvestor && onOpenInvestor(`${h.id_scheme}:${h.shareholder_id}`)} className="cursor-pointer hover:bg-[var(--bg-elevated)]" title="کلیک: پرتفوی این سهام‌دار">
                        <td className="p-2 font-semibold">{h.shareholder_name}{h._merged > 1 ? <span className="text-[10px] text-[var(--text-secondary)]"> ({toFa(h._merged)} رکورد یک‌تا شد)</span> : ''}</td>
                        <td className="p-2">{h.shareholder_type === 'legal' ? 'حقوقی' : h.shareholder_type === 'individual' ? 'حقیقی' : '—'}</td>
                        <td className="p-2 num">{fmtNum(h.shares)}</td>
                        <td className="p-2 num">
                          <div>{fmtPct(h.ownership_percent_reported)}</div>
                          <div className="text-[var(--text-secondary)] text-[10px]">محاسبه: {fmtPct(h.ownership_percent_calculated)}</div>
                        </td>
                        <td className={`p-2 num ${h.shares_change > 0 ? 'text-[var(--up)]' : h.shares_change < 0 ? 'text-[var(--down)]' : ''}`}>
                          {h.shares_change === null ? (h.entry_status === 'first_observed_unconfirmed' ? 'اولین مشاهده' : '—') : `${h.shares_change > 0 ? '+' : ''}${fmtNum(h.shares_change)}`}
                        </td>
                        <td className="p-2 num">{h.market_value !== null ? fmtMoney(h.market_value, currency) : (h.price !== null && h.shares !== null ? fmtMoney(h.shares * h.price, currency) : '—')}</td>
                        <td className="p-2"><IdentityBadge status={h.identity_status} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {tab === 'holders' && (
            <div className="rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] overflow-x-auto">
              <div className="p-2.5 border-b border-[var(--border-subtle)] flex flex-wrap justify-between items-center gap-2 text-xs">
                <div className="flex items-center gap-2">
                  <span className="font-bold">ترکیب سهام‌داران عمده در تاریخ:</span>
                  <select value={selectedDate || holdersRes.date_jalali || ''} onChange={e => setSelectedDate(e.target.value)} className="px-2 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] num">
                    {detail.available_dates.map(d => <option key={d} value={d}>{fmtDate(d)}</option>)}
                  </select>
                  {mergedHoldersCount > 0 && <span className="text-[var(--text-secondary)]">({toFa(dedupedHolders.length)} سهام‌دار یک‌تا‌شده از {toFa(holdersRes.rows.length)} رکورد — رکوردهای تکراری همان سهام‌دار با چند شناسه یکی شده‌اند)</span>}
                  {unquantifiedNames.length > 0 && <span className="text-amber-300">بدون دادهٔ تعداد سهم (در شمار سهام‌داران عمده نمی‌آیند): {unquantifiedNames.join('، ')}</span>}
                </div>
                <div className="flex gap-2">
                  <button onClick={() => setSortBy('shares')} className={`px-2 py-0.5 rounded border border-[var(--border-subtle)] ${sortBy === 'shares' ? 'bg-blue-600 text-white' : ''}`}>مرتب‌سازی: سهام</button>
                  <button onClick={() => setSortBy('change')} className={`px-2 py-0.5 rounded border border-[var(--border-subtle)] ${sortBy === 'change' ? 'bg-blue-600 text-white' : ''}`}>مرتب‌سازی: تغییر</button>
                </div>
              </div>
              <table className="w-full text-xs text-right">
                <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                  <tr>
                    <th className="p-2">نام سهام‌دار</th>
                    <th className="p-2">نوع</th>
                    <th className="p-2">شناسه و اسکیما</th>
                    <th className="p-2">تعداد سهام</th>
                    <th className="p-2">درصد مالکیت</th>
                    <th className="p-2">تغییر سهام</th>
                    <th className="p-2">قیمت و ارزش مبنا</th>
                    <th className="p-2">وضعیت هویت و ورود</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-subtle)]">
                  {dedupedHolders.map(h => (
                    <tr key={`${h.id_scheme}-${h.shareholder_id}`} onClick={() => onOpenInvestor && onOpenInvestor(`${h.id_scheme}:${h.shareholder_id}`)} className="cursor-pointer hover:bg-[var(--bg-elevated)]" title="کلیک: پرتفوی این سهام‌دار">
                      <td className="p-2 font-semibold">{h.shareholder_name}{h._merged > 1 ? <span className="text-[10px] text-[var(--text-secondary)]"> ({toFa(h._merged)} رکورد یک‌تا شد)</span> : ''}</td>
                      <td className="p-2">{h.shareholder_type === 'legal' ? 'حقوقی' : h.shareholder_type === 'individual' ? 'حقیقی' : '—'}</td>
                      <td className="p-2 text-[10px] num text-[var(--text-secondary)]">{h.id_scheme}: {h.shareholder_id}</td>
                      <td className="p-2 num">{fmtNum(h.shares)}</td>
                      <td className="p-2 num">
                        <div title="درصد گزارش‌شده در منبع">{fmtPct(h.ownership_percent_reported)}</div>
                        <div className="text-[var(--text-secondary)] text-[10px]" title="درصد محاسبه‌شده از تعداد سهام تقسیم بر کل سهام شرکت">محاسبه: {fmtPct(h.ownership_percent_calculated)}</div>
                      </td>
                      <td className={`p-2 num ${h.shares_change > 0 ? 'text-[var(--up)]' : h.shares_change < 0 ? 'text-[var(--down)]' : ''}`}>
                        {h.shares_change === null ? (h.entry_status === 'first_observed_unconfirmed' ? 'اولین مشاهده' : '—') : `${h.shares_change > 0 ? '+' : ''}${fmtNum(h.shares_change)}`}
                      </td>
                      <td className="p-2 num">
                        {h.market_value !== null ? fmtMoney(h.market_value, currency) : (h.price !== null && h.shares !== null ? fmtMoney(h.shares * h.price, currency) : '—')}
                        {h.valuation_status === 'reference_price_no_trade_on_date' && <div className="text-[10px] text-amber-400">قیمت مرجع بدون معامله</div>}
                      </td>
                      <td className="p-2 space-y-1">
                        <div><IdentityBadge status={h.identity_status} /></div>
                        {h.entry_status === 'first_observed_unconfirmed' && <div className="text-[10px] text-amber-300">اولین مشاهده در داده</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {tab === 'events' && (
            <div className="rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] overflow-x-auto text-xs">
              <table className="w-full text-right">
                <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                  <tr><th className="p-2">تاریخ</th><th className="p-2">نام سهام‌دار</th><th className="p-2">نوع رویداد</th><th className="p-2">تغییر سهام</th><th className="p-2">درصد مالکیت</th><th className="p-2">ارزش روز رویداد</th></tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-subtle)]">
                  {(seriesRes.events || []).map((ev, i) => (
                    <tr key={i}>
                      <td className="p-2 num">{fmtDate(ev.date_jalali)}</td>
                      <td className="p-2">{ev.shareholder_name}</td>
                      <td className="p-2 font-semibold">{CHANGE_TYPE_FA[ev.change_type] || ev.change_type}</td>
                      <td className={`p-2 num ${ev.shares_change > 0 ? 'text-[var(--up)]' : ev.shares_change < 0 ? 'text-[var(--down)]' : ''}`}>{fmtNum(ev.shares_change)}</td>
                      <td className="p-2 num">{fmtPct(ev.ownership_percent_calculated)}</td>
                      <td className="p-2 num">{ev.value_rial !== null && ev.value_rial !== undefined ? fmtMoney(ev.value_rial, currency) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {tab === 'actions' && (
            <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] text-xs">
              <div className="mb-2 text-[var(--text-secondary)]">
                افزایش سرمایهٔ واقعی: <span className="font-bold text-[var(--text-primary)] num">{toFa(actions.filter(a => a.action_type === 'capital_increase').length)}</span>
                {' '}— تعدیل قیمت (تغییر قیمت مرجع، نه افزایش سرمایه): <span className="font-bold text-[var(--text-primary)] num">{toFa(actions.filter(a => a.action_type === 'price_adjustment').length)}</span>
              </div>
              {actions.length === 0 ? (
                <div className="p-4 text-center text-[var(--text-secondary)]">رویداد افزایش سرمایه‌ای در داده‌های این نماد ثبت نشده است.</div>
              ) : (
                <table className="w-full text-right">
                  <thead className="bg-[var(--bg-elevated)]"><tr><th className="p-2">تاریخ</th><th className="p-2">نوع</th><th className="p-2">سهام قدیم</th><th className="p-2">سهام جدید</th><th className="p-2">نسبت</th><th className="p-2">تأییدیه</th></tr></thead>
                  <tbody>
                    {actions.map((ca, i) => (
                      <tr key={i} className="border-t border-[var(--border-subtle)]">
                        <td className="p-2 num">{fmtDate(ca.event_date_jalali || ca.effective_date_jalali)}</td>
                        <td className="p-2">{ACTION_TYPE_FA[ca.action_type] || ca.action_type}</td>
                        <td className="p-2 num">{fmtNum(ca.old_total_shares)}</td>
                        <td className="p-2 num">{fmtNum(ca.new_total_shares)}</td>
                        <td className="p-2 num">{ca.capital_increase_ratio ?? ca.share_ratio_new_over_old ?? '—'}</td>
                        <td className="p-2">{ca.verification_status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}

          {tab === 'quality' && quality && (
            <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-2.5 text-xs">
              {snap.total_shareholder_count_note && (
                <div className="p-2.5 rounded bg-amber-500/10 border border-amber-500/30 text-amber-300">
                  <strong>توضیح تعداد کل سهام‌داران:</strong> {snap.total_shareholder_count_note}
                </div>
              )}
              <div className="font-bold">شکاف‌های داده (`data_gaps`: {toFa(quality.data_gaps.length)})</div>
              {quality.data_gaps.map((g, i) => (
                <div key={i} className="p-2 rounded bg-[var(--bg-elevated)] border border-[var(--border-subtle)]">
                  <strong>{g.field}</strong> ({fmtDate(g.date_from)} تا {fmtDate(g.date_to)}): {g.reason}
                </div>
              ))}
              <div className="font-bold pt-2">گزارش‌های تطبیق (`reconciliation`: {toFa(quality.reconciliation.length)})</div>
              {quality.reconciliation.slice(0, 30).map((r, i) => (
                <div key={i} className="p-2 rounded bg-[var(--bg-elevated)] border border-[var(--border-subtle)] flex justify-between">
                  <span><strong>[{r.category}]</strong> {r.description}</span>
                  <span className="num text-[var(--text-secondary)]">{fmtDate(r.date_jalali)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      );
    }

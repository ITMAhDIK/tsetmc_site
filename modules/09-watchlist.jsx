/* ============================================================================
 * MODULE 10 — 09-WATCHLIST
 * Responsibility: Persistent watchlist, autocomplete and sorting.
 * Load order: after 00-runtime.jsx and in the numeric order used by index HTML.
 * Shared contracts: React hooks and declarations from earlier modules are
 * available in this page. Keep API payloads and business rules unchanged here.
 * Maintenance: add focused comments beside non-obvious calculations, state
 * transitions, data provenance assumptions, and event handlers.
 * ========================================================================== */
// --- 9. Page 6: Persistent Watchlist with Autocomplete & Sorting ---
    function WatchlistPage({ onOpenStock, currency }) {
      const [items, setItems] = useState([]);
      const [symInput, setSymInput] = useState('');
      const [suggestions, setSuggestions] = useState([]);
      const [sortCol, setSortCol] = useState('disclosed_ownership_percent_sum');
      const [sortAsc, setSortAsc] = useState(false);
      const [err, setErr] = useState('');
      const [msg, setMsg] = useState('');

      const load = () => apiFetch('/api/v1/watchlists/default').then(res => setItems(res.items || []));
      useEffect(() => { load(); }, []);

      useEffect(() => {
        if (!symInput.trim()) { setSuggestions([]); return; }
        const t = setTimeout(() => {
          apiFetch(`/api/v1/stocks/search?q=${encodeURIComponent(symInput.trim())}&limit=8`)
            .then(r => setSuggestions(r.results || []))
            .catch(() => setSuggestions([]));
        }, 200);
        return () => clearTimeout(t);
      }, [symInput]);

      const addSym = (targetSym) => {
        const sym = (targetSym || symInput).trim();
        if (!sym) { setErr('نمادی وارد نشده است.'); setMsg(''); return; }
        apiFetch('/api/v1/watchlists/default/symbols', { method: 'POST', body: JSON.stringify({ symbol: sym }) })
          .then(() => { setSymInput(''); setSuggestions([]); setErr(''); setMsg(`نماد ${sym} به دیده‌بان اضافه شد.`); load(); })
          .catch(e => { setErr(e.message); setMsg(''); });
      };

      const removeSym = (sym) => {
        apiFetch(`/api/v1/watchlists/default/symbols/${encodeURIComponent(sym)}`, { method: 'DELETE' }).then(load);
      };

      const sortedItems = useMemo(() => {
        return [...items].sort((a, b) => {
          const va = a[sortCol] ?? 0, vb = b[sortCol] ?? 0;
          if (typeof va === 'string') return sortAsc ? va.localeCompare(vb, 'fa') : vb.localeCompare(va, 'fa');
          return sortAsc ? va - vb : vb - va;
        });
      }, [items, sortCol, sortAsc]);

      const toggleSort = (col) => {
        if (sortCol === col) setSortAsc(!sortAsc);
        else { setSortCol(col); setSortAsc(false); }
      };

      return (
        <div className="space-y-3 text-xs">
          <ErrorBox error={err} />
          {msg && <div className="p-2.5 rounded border border-emerald-500/40 bg-emerald-500/10 text-emerald-300 text-xs">{msg}</div>}
          <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] flex flex-wrap justify-between items-center gap-2">
            <h1 className="text-sm font-bold">دیده‌بان شخصی (ذخیره‌شده در SQLite)</h1>
            <div className="flex gap-2 relative">
              <input value={symInput} onChange={e => { setSymInput(e.target.value); setMsg(''); }} onKeyDown={e => { if (e.key === 'Enter') addSym(); }} placeholder="جستجو و افزودن نماد... (Enter)" className="px-2.5 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] w-56" />
              <button onClick={() => addSym()} className="px-3 py-1 rounded bg-blue-600 text-white">+ افزودن</button>
              {suggestions.length > 0 && (
                <div className="absolute right-0 top-9 w-64 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] shadow-xl z-40">
                  {suggestions.map(s => (
                    <button key={s.symbol} onClick={() => addSym(s.symbol)} className="w-full text-right p-2 hover:bg-[var(--bg-elevated)] flex justify-between">
                      <span className="font-bold text-blue-400">{s.symbol}</span>
                      <span className="text-[var(--text-secondary)] truncate">{s.company_name || s.market}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
          <div className="rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] overflow-x-auto">
            <table className="w-full text-right">
              <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                <tr>
                  <th className="p-2 cursor-pointer" onClick={() => toggleSort('symbol')}>نماد ⇅</th>
                  <th className="p-2">نام شرکت</th>
                  <th className="p-2">بازار</th>
                  <th className="p-2 cursor-pointer" onClick={() => toggleSort('disclosed_shareholder_count')}>تعداد عمده ⇅</th>
                  <th className="p-2 cursor-pointer" onClick={() => toggleSort('disclosed_ownership_percent_sum')}>مالکیت عمده٪ ⇅</th>
                  <th className="p-2 cursor-pointer" onClick={() => toggleSort('market_value')}>ارزش بازار ⇅</th>
                  <th className="p-2">حذف</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {sortedItems.map(s => (
                  <tr key={s.symbol}>
                    <td className="p-2 font-bold text-blue-400 cursor-pointer" onClick={() => onOpenStock(s.symbol)}>{s.symbol}</td>
                    <td className="p-2">{s.company_name || '—'}</td>
                    <td className="p-2">{s.market}</td>
                    <td className="p-2 num">{toFa(s.deduped_shareholder_count ?? s.disclosed_shareholder_count)}</td>
                    <td className="p-2 num">{s.deduped_ownership_percent_sum === null || s.deduped_ownership_percent_sum === undefined ? 'نامشخص' : (Number(s.deduped_ownership_percent_sum) > 100.000001 || Number(s.deduped_ownership_percent_sum) < 0) ? <span className="text-amber-400" title="مجموع مالکیت پس از حذف موارد اثبات‌شدهٔ تکرار و محاسبه با مخرج همان تاریخ هنوز ناسازگار است؛ برای تحلیل معتبر نیست.">نیازمند بررسی</span> : fmtPct(s.deduped_ownership_percent_sum)}</td>
                    <td className="p-2 num">{s.market_value !== null && s.market_value !== undefined ? fmtMoney(s.market_value, currency) : '—'}</td>
                    <td className="p-2"><button onClick={() => removeSym(s.symbol)} className="text-red-400">حذف</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      );
    }

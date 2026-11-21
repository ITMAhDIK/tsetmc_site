/* ============================================================================
 * MODULE 09 — 08-SCREENER
 * Responsibility: Multi-stock screener filters, query state and results table.
 * Load order: after 00-runtime.jsx and in the numeric order used by index HTML.
 * Shared contracts: React hooks and declarations from earlier modules are
 * available in this page. Keep API payloads and business rules unchanged here.
 * Maintenance: add focused comments beside non-obvious calculations, state
 * transitions, data provenance assumptions, and event handlers.
 * ========================================================================== */
// --- 8. Page 5: Multi-Stock Screener (All Supported Columns) ---
    // Enum fields get a SELECT (op fixed to '='); dates get a date input;
    // numbers get a number input. The real vocabularies come from the
    // pipeline (identity_status, data_completeness, entry_status).
    const SCREENER_ENUMS = {
      shareholder_type: [['legal', 'حقوقی'], ['individual', 'حقیقی']],
      market: [['TSE', 'بورس (TSE)'], ['IFB', 'فرابورس (IFB)'], ['UNKNOWN', 'تعیین‌نشده']],
      change_type: [['new', 'جدید'], ['increase', 'افزایش'], ['decrease', 'کاهش'], ['unchanged', 'بدون تغییر'], ['unknown', 'نامشخص']],
      entry_status: [['first_observed_unconfirmed', 'اولین مشاهده (تأییدنشده)'], ['reappeared_after_not_disclosed', 'بازگشت پس از عدم افشا']],
      identity_status: [
        ['verified_same_source_row', 'تأییدشده (هم‌ردیف منبع)'],
        ['inferred_exact_match', 'تطبیق دقیق استنتاجی'],
        ['unlinked_record_id', 'شناسه منفرد (تأییدنشده)'],
        ['ambiguous_not_linked', 'مبهم / پیوند نشده'],
        ['source_id', 'بدون اطلاعات پیوند (منبع)'],
      ],
      data_completeness: [
        ['complete_by_reported_percent', 'کامل (بر اساس درصد گزارش‌شده)'],
        ['partial_major_holders_only', 'فقط سهام‌داران عمده'],
        ['inconsistent_sum_over_100_percent', 'جمع مالکیت بیش از ۱۰۰٪'],
        ['no_holder_observations', 'بدون مشاهده سهام‌دار'],
        ['holders_only_total_shares_unavailable', 'بدون کل سهام شرکت'],
      ],
    };
    const SCREENER_FIELDS = [
      ['ownership_percent_calculated', 'درصد مالکیت', 'number'],
      ['disclosed_shareholder_count', 'تعداد سهام‌دار عمده', 'number'],
      ['disclosed_ownership_percent_sum', 'تمرکز مالکیت عمده٪', 'number'],
      ['shares_change', 'تغییر سهام', 'number'],
      ['change_type', 'نوع تغییر', 'enum'],
      ['entry_status', 'وضعیت ورود', 'enum'],
      ['shareholder_type', 'نوع سهام‌دار', 'enum'],
      ['identity_status', 'وضعیت احراز هویت', 'enum'],
      ['data_completeness', 'تکمیل بودن داده', 'enum'],
      ['market', 'بازار', 'enum'],
      ['has_corporate_action', 'تعداد افزایش سرمایه', 'number'],
      ['date_gregorian', 'تاریخ میلادی (YYYY-MM-DD)', 'date'],
    ];
    function ScreenerPage({ onOpenStock }) {
      const [logic, setLogic] = useState('AND');
      const [conds, setConds] = useState([
        { field: 'ownership_percent_calculated', op: '>=', value: 5 },
      ]);
      const [sortBy, setSortBy] = useState('ownership_percent_calculated');
      const [results, setResults] = useState([]);
      const [saved, setSaved] = useState([]);
      const [saveName, setSaveName] = useState('');
      const [loading, setLoading] = useState(false);

      const runQuery = useCallback(() => {
        setLoading(true);
        apiFetch('/api/v1/screener/query', {
          method: 'POST',
          body: JSON.stringify({ logic, conditions: conds, sort_by: sortBy, sort_dir: 'DESC', limit: 100 }),
        })
          .then(res => setResults(res.rows || []))
          .finally(() => setLoading(false));
      }, [logic, conds, sortBy]);

      useEffect(() => {
        runQuery();
        apiFetch('/api/v1/screener/saved-filters').then(res => setSaved(res.filters || [])).catch(() => {});
      }, []);

      const saveCurrent = () => {
        if (!saveName.trim()) return;
        apiFetch('/api/v1/screener/saved-filters', {
          method: 'POST',
          body: JSON.stringify({ name: saveName.trim(), filter_ast: { logic, conditions: conds, sort_by: sortBy, sort_dir: 'DESC' } }),
        }).then(() => {
          setSaveName('');
          apiFetch('/api/v1/screener/saved-filters').then(res => setSaved(res.filters || []));
        });
      };

      const exportCsv = () => {
        fetch(`${API_BASE}/api/v1/screener/export`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ logic, conditions: conds, sort_by: sortBy, sort_dir: 'DESC' }),
        })
          .then(r => r.blob())
          .then(blob => {
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = 'tsetmc_screener.csv'; a.click();
            URL.revokeObjectURL(url);
          });
      };

      return (
        <div className="space-y-3 text-xs">
          <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-2.5">
            <div className="flex flex-wrap justify-between items-center gap-2">
              <h1 className="text-sm font-bold">فیلتر پیشرفته روی کل نمادها</h1>
              <div className="flex flex-wrap gap-2">
                <select value={logic} onChange={e => setLogic(e.target.value)} className="px-2 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] font-bold">
                  <option value="AND">AND (همه شرط‌ها)</option>
                  <option value="OR">OR (حداقل یک شرط)</option>
                </select>
                <button onClick={() => setConds(p => [...p, { field: 'shareholder_type', op: '=', value: 'legal' }])} className="px-2.5 py-1 rounded bg-[var(--bg-elevated)] border border-[var(--border-subtle)]">+ شرط</button>
                <button onClick={runQuery} className="px-3 py-1 rounded bg-blue-600 text-white font-semibold">اجرای فیلتر</button>
                <button onClick={exportCsv} className="px-2.5 py-1 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)]">خروجی CSV</button>
              </div>
            </div>

            {conds.map((c, idx) => {
              const fieldMeta = SCREENER_FIELDS.find(f => f[0] === c.field) || SCREENER_FIELDS[0];
              const kind = fieldMeta[2];
              const enumOptions = SCREENER_ENUMS[c.field];
              const setCond = (patch) => setConds(p => p.map((x, i) => i === idx ? { ...x, ...patch } : x));
              return (
              <div key={idx} className="flex flex-wrap gap-2 items-center p-2 rounded bg-[var(--bg-elevated)]">
                <select value={c.field} onChange={e => {
                  const nf = e.target.value;
                  const nk = (SCREENER_FIELDS.find(f => f[0] === nf) || [])[2];
                  if (nk === 'enum') setCond({ field: nf, op: '=', value: (SCREENER_ENUMS[nf] || [['', '']])[0][0] });
                  else setCond({ field: nf, op: '>=', value: '' });
                }} className="px-2 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)]">
                  {SCREENER_FIELDS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
                </select>
                {kind === 'enum' ? (
                  <span className="px-2 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] text-[var(--text-secondary)]">=</span>
                ) : (
                  <select value={c.op} onChange={e => setCond({ op: e.target.value })} className="px-2 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)]">
                    <option value=">=">&gt;=</option>
                    <option value="<=">&lt;=</option>
                    <option value="=">=</option>
                    <option value=">">&gt;</option>
                    <option value="<">&lt;</option>
                  </select>
                )}
                {kind === 'enum' ? (
                  <select value={c.value} onChange={e => setCond({ value: e.target.value })} className="px-2 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] w-64">
                    {(enumOptions || []).map(([v, label]) => <option key={v} value={v}>{label}</option>)}
                  </select>
                ) : kind === 'date' ? (
                  <input type="date" value={c.value || ''} onChange={e => setCond({ value: e.target.value })} className="px-2 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] w-40 num" />
                ) : (
                  <input type="number" value={c.value} onChange={e => {
                    const v = e.target.value;
                    setCond({ value: v === '' ? '' : Number(v) });
                  }} className="px-2 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] w-40 num" placeholder="عدد..." />
                )}
                <button onClick={() => setConds(p => p.filter((_, i) => i !== idx))} className="text-red-400">حذف</button>
              </div>
              );
            })}

            <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-[var(--border-subtle)]">
              <input value={saveName} onChange={e => setSaveName(e.target.value)} placeholder="نام فیلتر..." className="px-2 py-1 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)]" />
              <button onClick={saveCurrent} className="px-2.5 py-1 rounded bg-emerald-600 text-white">ذخیره در دیتابیس</button>
              {saved.map(sf => (
                <button key={sf.id} onClick={() => {
                  const ast = JSON.parse(sf.filter_ast_json);
                  setLogic(ast.logic); setConds(ast.conditions);
                }} className="px-2 py-0.5 rounded bg-[var(--bg-elevated)] border border-[var(--border-subtle)]">{sf.name}</button>
              ))}
            </div>
          </div>

          <div className="rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] overflow-x-auto">
            <table className="w-full text-right">
              <thead className="bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                <tr><th className="p-2">نماد</th><th className="p-2">بازار</th><th className="p-2">سهام‌دار</th><th className="p-2">درصد مالکیت</th><th className="p-2">تغییر سهام</th><th className="p-2">تمرکز شرکت٪</th></tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {loading ? <tr><td colSpan="6" className="p-6 text-center">در حال اجرای فیلتر...</td></tr> : results.map((r, i) => (
                  <tr key={i} onClick={() => onOpenStock(r.symbol)} className="hover:bg-[var(--bg-elevated)] cursor-pointer">
                    <td className="p-2 font-bold text-blue-400">{r.symbol}</td>
                    <td className="p-2">{r.market}</td>
                    <td className="p-2">{r.shareholder_name}</td>
                    <td className="p-2 num font-semibold">{fmtPct(r.ownership_percent_calculated)}</td>
                    <td className="p-2 num">{fmtNum(r.shares_change)}</td>
                    <td className="p-2 num">{fmtPct(r.disclosed_ownership_percent_sum)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      );
    }

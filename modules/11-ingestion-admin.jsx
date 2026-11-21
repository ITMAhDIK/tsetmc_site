/* ============================================================================
 * MODULE 12 — 11-INGESTION-ADMIN
 * Responsibility: Data-ingestion administration dashboard and operational status UI.
 * Load order: after 00-runtime.jsx and in the numeric order used by index HTML.
 * Shared contracts: React hooks and declarations from earlier modules are
 * available in this page. Keep API payloads and business rules unchanged here.
 * Maintenance: add focused comments beside non-obvious calculations, state
 * transitions, data provenance assumptions, and event handlers.
 * ========================================================================== */
// --- 11. Page 8: Real Ingestion Administration Panel ---
    function AdminDashboardPage() {
      const [overview, setOverview] = useState(null);
      const [manifests, setManifests] = useState([]);
      const [errors, setErrors] = useState([]);
      const [queue, setQueue] = useState([]);
      const [statusFilter, setStatusFilter] = useState('ALL');
      const [manifestQ, setManifestQ] = useState('');
      const [msg, setMsg] = useState('');

      const refreshAll = useCallback(() => {
        apiFetch('/api/v1/admin/overview').then(setOverview);
        apiFetch(`/api/v1/admin/manifests?status=${statusFilter}&q=${encodeURIComponent(manifestQ)}&limit=50`).then(r => setManifests(r.rows || []));
        apiFetch('/api/v1/admin/errors?limit=30').then(r => setErrors(r.errors || []));
        apiFetch('/api/v1/admin/identity/queue?limit=30').then(r => setQueue(r.queue || []));
      }, [statusFilter, manifestQ]);

      useEffect(() => { refreshAll(); }, [refreshAll]);

      const trigger = (retryFailed) => {
        apiFetch(`/api/v1/admin/ingestion/trigger?retry_failed=${retryFailed}`, { method: 'POST' })
          .then(res => { setMsg(`پایپلاین ایمپورت با PID ${res.pid} شروع شد.`); refreshAll(); })
          .catch(e => setMsg(e.message));
      };

      const resolveLink = (linkId, decision) => {
        apiFetch('/api/v1/admin/identity/resolve', {
          method: 'POST',
          body: JSON.stringify({ link_id: linkId, decision, reason: 'Manual review from Admin panel' }),
        }).then(() => refreshAll());
      };

      const mBySt = overview?.manifests_by_status || {};
      const tot = overview?.totals || {};
      const duck = overview?.sqlite_health || {};
      const run = overview?.latest_run;
      const doneInRun = run ? (run.processed_files + run.failed_files) : 0;
      const pctRun = run && run.total_files > 0 ? Math.min(100, Math.round((doneInRun / run.total_files) * 100)) : 0;

      return (
        <div className="space-y-3 text-xs">
          {msg && <div className="p-2 rounded bg-blue-500/15 text-blue-300">{msg}</div>}

          <div className="grid grid-cols-2 md:grid-cols-6 gap-2">
            {[
              ['فایل‌های کشف‌شده', fmtNum(tot.total_discovered_files ?? 0)],
              ['تکمیل‌شده (Completed)', fmtNum(mBySt.completed ?? 0)],
              ['ناموفق (Failed)', fmtNum(mBySt.failed ?? 0)],
              ['نمادهای یکتا (SQLite)', fmtNum(duck.unique_symbols ?? 0)],
              ['رکوردهای مالکیت', fmtNum(duck.holdings_records ?? 0)],
              ['وضعیت SQLite', duck.healthy ? 'آنلاین' : 'خطا'],
            ].map(([k, v]) => (
              <div key={k} className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
                <div className="text-[var(--text-secondary)]">{k}</div>
                <div className="font-bold mt-1 num">{v}</div>
              </div>
            ))}
          </div>

          <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-2">
            <div className="flex flex-wrap justify-between items-center gap-2">
              <div>
                آخرین ایمپورت موفق: <span className="num">{tot.last_successful_ingestion || '—'}</span> | وضعیت اجرا: <strong>{overview?.ingestion_subprocess_running ? 'در حال پردازش...' : 'آماده'}</strong>
              </div>
              <div className="flex gap-2">
                <button onClick={refreshAll} className="px-2.5 py-1 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)]">به‌روزرسانی</button>
                <button onClick={() => trigger(false)} className="px-3 py-1 rounded bg-blue-600 text-white">شروع / ادامه ایمپورت</button>
                <button onClick={() => trigger(true)} className="px-3 py-1 rounded bg-amber-600 text-white">پردازش مجدد خطاها</button>
              </div>
            </div>
            {run && (
              <div className="space-y-1">
                <div className="flex justify-between text-[11px] text-[var(--text-secondary)]">
                  <span>پیشرفت آخرین اجرا ({run.status}): <span className="num">{fmtNum(doneInRun)} / {fmtNum(run.total_files)}</span> (رد شده بدون تغییر: <span className="num">{fmtNum(run.skipped_files)}</span>)</span>
                  <span className="num font-bold">{toFa(pctRun)}٪</span>
                </div>
                <div className="w-full h-2 rounded bg-[var(--bg-canvas)] overflow-hidden">
                  <div style={{ width: `${pctRun}%` }} className="h-full bg-blue-500 transition-all" />
                </div>
              </div>
            )}
          </div>

          <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-2">
            <div className="flex flex-wrap justify-between items-center gap-2">
              <h2 className="font-bold">مانیفست فایل‌ها (`ingestion_manifests`)</h2>
              <div className="flex flex-wrap gap-1.5">
                <input value={manifestQ} onChange={e => setManifestQ(e.target.value)} placeholder="جستجوی فایل یا نماد..." className="px-2 py-0.5 rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)]" />
                {['ALL', 'completed', 'pending', 'processing', 'failed'].map(st => (
                  <button key={st} onClick={() => setStatusFilter(st)} className={`px-2 py-0.5 rounded border border-[var(--border-subtle)] ${statusFilter === st ? 'bg-blue-600 text-white' : ''}`}>{st}</button>
                ))}
              </div>
            </div>
            <div className="overflow-x-auto max-h-60 overflow-y-auto">
              <table className="w-full text-right">
                <thead className="bg-[var(--bg-elevated)]"><tr><th className="p-2">فایل</th><th className="p-2">نماد</th><th className="p-2">رکوردهای مالکیت</th><th className="p-2">مرحله</th><th className="p-2">وضعیت</th><th className="p-2">خطا</th></tr></thead>
                <tbody className="divide-y divide-[var(--border-subtle)]">
                  {manifests.map(m => (
                    <tr key={m.file_path}><td className="p-2 num">{m.file_name}</td><td className="p-2 font-bold">{m.symbol || '—'}</td><td className="p-2 num">{fmtNum(m.holdings_rows)}</td><td className="p-2">{m.last_stage}</td><td className="p-2">{m.status}</td><td className="p-2 text-red-400">{m.last_error || '—'}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-2">
              <h2 className="font-bold">صف تأیید هویت سهام‌داران ({toFa(queue.length)})</h2>
              <div className="max-h-60 overflow-y-auto space-y-1.5">
                {queue.map(qItem => (
                  <div key={qItem.id} className="p-2 rounded bg-[var(--bg-elevated)] flex justify-between items-center gap-2">
                    <div>
                      <div className="font-semibold">{qItem.observed_name}</div>
                      <div className="text-[10px] text-[var(--text-secondary)] num">{qItem.symbol} | {qItem.id_scheme}:{qItem.raw_source_id} ({qItem.link_status})</div>
                    </div>
                    <div className="flex gap-1">
                      <button onClick={() => resolveLink(qItem.id, 'verified')} className="px-2 py-0.5 rounded bg-emerald-600 text-white">تأیید</button>
                      <button onClick={() => resolveLink(qItem.id, 'conflicting')} className="px-2 py-0.5 rounded bg-red-600 text-white">تعارض</button>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="p-3 rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] space-y-2">
              <h2 className="font-bold">لاگ خطاهای ایمپورت ({toFa(errors.length)})</h2>
              <div className="max-h-60 overflow-y-auto space-y-1.5">
                {errors.map(er => (
                  <div key={er.id} className="p-2 rounded bg-[var(--bg-elevated)] border border-red-500/20">
                    <div className="font-semibold text-red-400">{er.file_name} {er.sheet_name ? `(${er.sheet_name})` : ''} {er.row_number ? `سطر ${toFa(er.row_number)}` : ''}</div>
                    <div className="text-[11px]">{er.error_message}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      );
    }

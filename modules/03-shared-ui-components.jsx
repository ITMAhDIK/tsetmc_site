/* ============================================================================
 * MODULE 04 — 03-SHARED-UI-COMPONENTS
 * Responsibility: Reusable React UI primitives, including chart rendering and drawing interactions.
 * Load order: after 00-runtime.jsx and in the numeric order used by index HTML.
 * Shared contracts: React hooks and declarations from earlier modules are
 * available in this page. Keep API payloads and business rules unchanged here.
 * Maintenance: add focused comments beside non-obvious calculations, state
 * transitions, data provenance assumptions, and event handlers.
 * ========================================================================== */
// --- 3. Reusable UI Components ---
    // Chart with: area/line/candlestick/bar types, event markers, Jalali
    // axis ticks (per DATE_MODE), right-edge anchoring (zooming out never
    // leaves empty space on the right), fullscreen, and simple drawing
    // tools (trend line / rectangle / horizontal line) as an SVG overlay.
    // Markers + ohlc are memoized by the parent (JSON key) so the chart
    // is not rebuilt on every render.
    function LWChart({ data, ohlc, type = 'area', height = 250, theme = 'dark', color = '#2962ff', unitLabel = '٪', markers = [], dateMode = 'jalali', enableDrawing = false, allowFullscreen = true, onHover = null }) {
      const ref = useRef(null);
      const wrapRef = useRef(null);
      const chartRef = useRef(null);
      const seriesRef = useRef(null);
      const onHoverRef = useRef(onHover);
      onHoverRef.current = onHover;
      const [full, setFull] = useState(false);
      const [tool, setTool] = useState(null);
      const [drawings, setDrawings] = useState([]);
      const [draft, setDraft] = useState(null);
      // Scale controls (TradingView-style, bottom-right): A = auto-scale,
      // L = logarithmic price scale.
      const [logScale, setLogScale] = useState(false);
      const [autoScale, setAutoScale] = useState(true);
      const [, setDrawVersion] = useState(0);
      const markersKey = JSON.stringify(markers || []);

      const cleanData = useMemo(() => {
        if (type === 'candlestick' || type === 'bar') {
          const rows = Array.isArray(ohlc) ? ohlc : (Array.isArray(data) ? data : []);
          const byTime = new Map();
          for (const item of rows) {
            if (!item || !item.time) continue;
            const o = Number(item.open), h = Number(item.high), l = Number(item.low), c = Number(item.close);
            if ([o, h, l, c].some(v => v === null || v === undefined || isNaN(v))) continue;
            byTime.set(String(item.time).slice(0, 10), { time: String(item.time).slice(0, 10), open: o, high: h, low: l, close: c });
          }
          return Array.from(byTime.values()).sort((a, b) => a.time.localeCompare(b.time));
        }
        if (!Array.isArray(data)) return [];
        const byTime = new Map();
        for (const item of data) {
          if (item && item.time && item.value !== null && item.value !== undefined && !isNaN(Number(item.value))) {
            byTime.set(String(item.time).slice(0, 10), { time: String(item.time).slice(0, 10), value: Number(item.value) });
          }
        }
        return Array.from(byTime.values()).sort((a, b) => a.time.localeCompare(b.time));
      }, [data, ohlc, type]);

      useEffect(() => {
        if (!ref.current || !window.LightweightCharts || cleanData.length === 0) return;
        const isLight = theme === 'light';
        const tickFmt = (time) => fmtGregorianDate(String(time).slice(0, 10));
        const chart = window.LightweightCharts.createChart(ref.current, {
          height,
          layout: {
            background: { type: 'solid', color: isLight ? '#ffffff' : '#131722' },
            textColor: isLight ? '#475569' : '#8b95a8',
            fontFamily: 'Vazirmatn, sans-serif',
          },
          grid: {
            vertLines: { color: isLight ? '#e2e8f0' : '#1e2536' },
            horzLines: { color: isLight ? '#e2e8f0' : '#1e2536' },
          },
          rightPriceScale: { borderColor: isLight ? '#cbd5e1' : '#242b3d', mode: logScale ? 1 : 0, autoScale },
          timeScale: { borderColor: isLight ? '#cbd5e1' : '#242b3d', timeVisible: false, tickMarkFormatter: tickFmt },
          localization: {
            priceFormatter: (p) => `${p.toFixed(2)} ${unitLabel}`,
            timeFormatter: (time) => fmtGregorianDate(String(time).slice(0, 10)),
          },
        });
        let series;
        if (type === 'candlestick') {
          series = chart.addCandlestickSeries({ upColor: '#00c853', downColor: '#ff3d00', wickUpColor: '#00c853', wickDownColor: '#ff3d00', borderVisible: false });
        } else if (type === 'bar') {
          series = chart.addBarSeries({ upColor: '#00c853', downColor: '#ff3d00' });
        } else if (type === 'line') {
          series = chart.addLineSeries({ color, lineWidth: 2 });
        } else {
          series = chart.addAreaSeries({ lineColor: color, topColor: `${color}55`, bottomColor: `${color}05`, lineWidth: 2 });
        }
        series.setData(cleanData);
        const parsedMarkers = JSON.parse(markersKey || '[]');
        if (parsedMarkers.length > 0) {
          try { series.setMarkers(parsedMarkers); } catch (e) { /* markers on missing times are ignored */ }
        }
        chart.timeScale().fitContent();
        // Right-edge anchor: zooming out must never leave empty space on
        // the right; clamp the visible range so the last bar stays at the
        // right edge (2 bars of breathing room).
        const lastIdx = cleanData.length - 1;
        const clampRight = (range) => {
          if (!range) return;
          if (range.to > lastIdx + 2) {
            const span = range.to - range.from;
            chart.timeScale().setVisibleLogicalRange({ from: lastIdx + 2 - span, to: lastIdx + 2 });
          }
        };
        chart.timeScale().subscribeVisibleLogicalRangeChange(clampRight);
        const bumpDraw = () => setDrawVersion(v => v + 1);
        chart.timeScale().subscribeVisibleTimeRangeChange(bumpDraw);
        chart.subscribeCrosshairMove(bumpDraw);
        if (onHoverRef.current) {
          chart.subscribeCrosshairMove((param) => {
            const cb = onHoverRef.current;
            if (!cb) return;
            if (!param || !param.time) { cb(null); return; }
            cb({
              time: String(param.time).slice(0, 10),
              x: param.point ? param.point.x : null,
              y: param.point ? param.point.y : null,
            });
          });
        }
        chartRef.current = chart;
        seriesRef.current = series;

        const ro = new ResizeObserver(() => {
          if (ref.current) chart.applyOptions({ width: ref.current.clientWidth });
        });
        ro.observe(ref.current);
        return () => { ro.disconnect(); chart.remove(); chartRef.current = null; seriesRef.current = null; };
      }, [cleanData, height, theme, color, unitLabel, type, markersKey, dateMode, logScale, autoScale]);

      // --- drawing overlay ---
      const pointFromEvent = (e) => {
        const chart = chartRef.current, series = seriesRef.current, wrap = wrapRef.current;
        if (!chart || !series || !wrap) return null;
        const rect = wrap.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        let time = null;
        try { time = chart.timeScale().coordinateToTime(x); } catch (err) { time = null; }
        let price = null;
        try { price = series.coordinateToPrice(y); } catch (err) { price = null; }
        if (time === null || time === undefined || price === null || price === undefined || isNaN(price)) return null;
        return { time: String(time).slice(0, 10), price };
      };
      const onOverlayDown = (e) => {
        if (!tool) return;
        const p = pointFromEvent(e);
        if (!p) return;
        if (tool === 'hline') {
          setDrawings(ds => [...ds, { id: Date.now() + Math.random(), kind: 'hline', price: p.price }]);
          setTool(null);
          return;
        }
        setDraft({ kind: tool, p1: p, p2: p });
      };
      const onOverlayMove = (e) => {
        if (!draft) return;
        const p = pointFromEvent(e);
        if (!p) return;
        setDraft(d => d ? { ...d, p2: p } : d);
      };
      const onOverlayUp = () => {
        if (!draft) return;
        setDrawings(ds => [...ds, { id: Date.now() + Math.random(), ...draft }]);
        setDraft(null);
      };
      const drawingXY = (d) => {
        const chart = chartRef.current, series = seriesRef.current;
        if (!chart || !series) return null;
        const toXY = (p) => {
          const x = chart.timeScale().timeToCoordinate(p.time);
          const y = series.priceToCoordinate(p.price);
          return (x === null || x === undefined || y === null || y === undefined) ? null : { x, y };
        };
        if (d.kind === 'hline') {
          const y = series.priceToCoordinate(d.price);
          return (y === null || y === undefined) ? null : { hlineY: y };
        }
        const a = toXY(d.p1), b = toXY(d.p2);
        return (a && b) ? { a, b } : null;
      };
      const renderDrawings = () => {
        const wrap = wrapRef.current;
        if (!wrap) return null;
        const W = wrap.clientWidth, H = wrap.clientHeight;
        const all = draft ? [...drawings, { ...draft, id: 'draft' }] : drawings;
        return (
          <svg className="absolute inset-0 pointer-events-none" width={W} height={H} style={{ zIndex: 5 }}>
            {all.map(d => {
              const xy = drawingXY(d);
              if (!xy) return null;
              if (d.kind === 'hline') {
                return (
                  <g key={d.id}>
                    <line x1={0} y1={xy.hlineY} x2={W} y2={xy.hlineY} stroke="#f59e0b" strokeWidth={1.5} strokeDasharray="6 3" />
                    <text x={W - 4} y={xy.hlineY - 4} fill="#f59e0b" fontSize={10} textAnchor="end">{toFa(Number(d.price).toFixed(2))}</text>
                  </g>
                );
              }
              if (d.kind === 'rect') {
                const x = Math.min(xy.a.x, xy.b.x), y = Math.min(xy.a.y, xy.b.y);
                return <rect key={d.id} x={x} y={y} width={Math.abs(xy.b.x - xy.a.x)} height={Math.abs(xy.b.y - xy.a.y)} fill="#2962ff22" stroke="#2962ff" strokeWidth={1.5} />;
              }
              return <line key={d.id} x1={xy.a.x} y1={xy.a.y} x2={xy.b.x} y2={xy.b.y} stroke="#00c853" strokeWidth={2} />;
            })}
          </svg>
        );
      };

      if (!window.LightweightCharts) return <div className="p-4 text-xs text-amber-400">کتابخانه نمودار بارگذاری نشد.</div>;
      if (cleanData.length === 0) return <div className="p-6 text-center text-xs text-[var(--text-secondary)]">سری زمانی معتبری برای رسم نمودار در این بازه موجود نیست.</div>;
      return (
        <div ref={wrapRef} className={full ? 'fixed inset-0 z-[100] bg-[var(--bg-canvas)] p-4 flex flex-col' : 'relative w-full'}>
          {(enableDrawing || allowFullscreen) && (
            <div className="flex flex-wrap gap-1.5 items-center mb-1.5 text-[11px]">
              {allowFullscreen && (
                <button onClick={() => setFull(f => !f)} className="px-2 py-0.5 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)]">
                  {full ? '✕ خروج از تمام‌صفحه' : '⛶ تمام‌صفحه'}
                </button>
              )}
              {enableDrawing && (
                <>
                  <span className="text-[var(--text-secondary)]">ابزار ترسیم:</span>
                  <button onClick={() => setTool(t => t === 'line' ? null : 'line')} className={`px-2 py-0.5 rounded border border-[var(--border-subtle)] ${tool === 'line' ? 'bg-emerald-600 text-white' : 'bg-[var(--bg-elevated)]'}`}>╲ خط روند</button>
                  <button onClick={() => setTool(t => t === 'rect' ? null : 'rect')} className={`px-2 py-0.5 rounded border border-[var(--border-subtle)] ${tool === 'rect' ? 'bg-blue-600 text-white' : 'bg-[var(--bg-elevated)]'}`}>▭ مستطیل</button>
                  <button onClick={() => setTool(t => t === 'hline' ? null : 'hline')} className={`px-2 py-0.5 rounded border border-[var(--border-subtle)] ${tool === 'hline' ? 'bg-amber-600 text-white' : 'bg-[var(--bg-elevated)]'}`}>― خط افقی</button>
                  <button onClick={() => setDrawings(ds => ds.slice(0, -1))} disabled={drawings.length === 0} className="px-2 py-0.5 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)] disabled:opacity-40">↩ بازگشت</button>
                  <button onClick={() => setDrawings([])} disabled={drawings.length === 0} className="px-2 py-0.5 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)] disabled:opacity-40">✕ پاک کردن ترسیم‌ها</button>
                  {tool && <span className="text-amber-300">روی نمودار بکشید (ابزار فعال: {tool === 'line' ? 'خط روند' : tool === 'rect' ? 'مستطیل' : 'خط افقی'})</span>}
                </>
              )}
            </div>
          )}
          <div className="relative flex-1" style={full ? { minHeight: 0 } : undefined}>
            <div ref={ref} className="w-full" dir="ltr" />
            <div className="absolute bottom-1 right-1 z-10 flex gap-1" dir="ltr">
              <button title="مقیاس خودکار (Auto scale)" onClick={() => setAutoScale(a => !a)} className={`w-6 h-6 rounded border text-[11px] font-bold ${autoScale ? 'bg-blue-600 text-white border-blue-600' : 'bg-[var(--bg-elevated)] border-[var(--border-subtle)]'}`}>A</button>
              <button title="مقیاس لگاریتمی (Log scale)" onClick={() => setLogScale(l => !l)} className={`w-6 h-6 rounded border text-[11px] font-bold ${logScale ? 'bg-blue-600 text-white border-blue-600' : 'bg-[var(--bg-elevated)] border-[var(--border-subtle)]'}`}>L</button>
            </div>
            {enableDrawing && (
              <div
                className="absolute inset-0"
                style={{ zIndex: 4, pointerEvents: tool || draft ? 'auto' : 'none', cursor: tool ? 'crosshair' : 'default' }}
                onMouseDown={onOverlayDown}
                onMouseMove={onOverlayMove}
                onMouseUp={onOverlayUp}
                onMouseLeave={() => setDraft(null)}
              />
            )}
            {renderDrawings()}
          </div>
        </div>
      );
    }

    const IdentityBadge = ({ status }) => {
      const map = {
        verified_same_source_row: { text: 'تأییدشده (هم‌ردیف منبع)', cls: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30' },
        inferred_exact_match: { text: 'تطبیق دقیق استنتاجی', cls: 'bg-blue-500/15 text-blue-400 border-blue-500/30' },
        unlinked_record_id: { text: 'شناسه منفرد (تأییدنشده)', cls: 'bg-amber-500/15 text-amber-400 border-amber-500/30' },
        ambiguous_not_linked: { text: 'مبهم / پیوند نشده', cls: 'bg-orange-500/15 text-orange-400 border-orange-500/30' },
        source_id: { text: 'بدون اطلاعات پیوند (منبع)', cls: 'bg-slate-500/15 text-slate-400 border-slate-500/30' },
      };
      const item = map[status];
      if (!item) return <span className="px-1.5 py-0.5 text-[10px] rounded border bg-slate-500/15 text-slate-400 border-slate-500/30">{status || '—'}</span>;
      return <span className={`px-1.5 py-0.5 text-[10px] rounded border ${item.cls}`}>{item.text}</span>;
    };

    const ErrorBox = ({ error }) => error ? (
      <div className="p-3 rounded border border-red-500/40 bg-red-500/10 text-red-300 text-xs">{error}</div>
    ) : null;

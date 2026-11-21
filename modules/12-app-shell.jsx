/* ============================================================================
 * MODULE 13 — 12-APP-SHELL
 * Responsibility: Root App state, navigation, shared controls and React mount.
 * Load order: after 00-runtime.jsx and in the numeric order used by index HTML.
 * Shared contracts: React hooks and declarations from earlier modules are
 * available in this page. Keep API payloads and business rules unchanged here.
 * Maintenance: add focused comments beside non-obvious calculations, state
 * transitions, data provenance assumptions, and event handlers.
 * ========================================================================== */
// --- 12. Root Shell ---
    const NAV_PAGES = [
      { id: 'market', label: '۱. داشبورد بازار' },
      { id: 'stock', label: '۲. صفحه نماد' },
      { id: 'shareholders', label: '۳. تحلیل سهام‌داران' },
      { id: 'portfolio', label: '۴. پرتفوی سرمایه‌گذاران' },
      { id: 'screener', label: '۵. فیلتر پیشرفته' },
      { id: 'watchlist', label: '۶. دیده‌بان' },
      { id: 'account', label: '۷. حساب و اشتراک' },
      { id: 'admin', label: '۸. پنل مدیریت ایمپورت' },
    ];

    function App() {
      const [page, setPage] = useState('market');
      const [activeSymbol, setActiveSymbol] = useState('');
      const [theme, setTheme] = useState('dark');
      const [currency, setCurrency] = useState('IRR');
      const [dateMode, setDateMode] = useState('jalali');
      // Bump after the global flips so every fmtDate/fmtGregorianDate call
      // re-renders with the new mode (the module-level DATE_MODE alone
      // does not trigger a render, which left tab-4 text dates stale).
      const [, setDateModeTick] = useState(0);
      const [portfolioInvestor, setPortfolioInvestor] = useState('');
      const openInvestor = (id) => { setPortfolioInvestor(id); setPage('portfolio'); };
      const [plan, setPlan] = useState('premium');
      const [quickSearch, setQuickSearch] = useState('');
      const [searchResults, setSearchResults] = useState([]);

      useEffect(() => { setDateModeGlobal(dateMode); setDateModeTick(t => t + 1); }, [dateMode]);
      const toggleDateMode = () => setDateMode(m => {
        const next = m === 'jalali' ? 'gregorian' : 'jalali';
        setDateModeGlobal(next);
        return next;
      });

      // Auto-select first imported stock so Tab 2 works even if شكبير3 was not in a pilot batch
      useEffect(() => {
        apiFetch('/api/v1/market/watch?limit=1')
          .then(res => {
            if (res.rows && res.rows.length > 0) {
              setActiveSymbol(prev => prev || res.rows[0].symbol);
            }
          })
          .catch(() => {});
      }, []);

      useEffect(() => {
        if (!quickSearch.trim()) { setSearchResults([]); return; }
        const t = setTimeout(() => {
          apiFetch(`/api/v1/stocks/search?q=${encodeURIComponent(quickSearch.trim())}`)
            .then(res => setSearchResults(res.results || []))
            .catch(() => setSearchResults([]));
        }, 200);
        return () => clearTimeout(t);
      }, [quickSearch]);

      const openStock = (sym) => {
        setActiveSymbol(sym);
        setPage('stock');
        setQuickSearch('');
        setSearchResults([]);
      };

      return (
        <div className={`app-shell min-h-screen flex flex-col bg-[var(--bg-canvas)] text-[var(--text-primary)] ${theme === 'light' ? 'theme-light' : ''}`}>
          <header className="app-header sticky top-0 z-40 border-b border-[var(--border-subtle)] bg-[var(--bg-panel)]">
            <div className="app-topbar max-w-[1600px] mx-auto px-3 flex items-center justify-between gap-3">
              <div className="app-brand-group flex items-center gap-4 relative">
                <button onClick={() => setPage('market')} className="brand-lockup whitespace-nowrap" aria-label="بازگشت به داشبورد بازار">
                  <span className="brand-symbol" aria-hidden="true"><span>ر</span></span>
                  <span className="brand-copy">
                    <span className="brand-name">ره‌نگار بورس</span>
                    <span className="brand-caption">هوشمندی بازار سرمایه</span>
                  </span>
                </button>
                <div className="relative">
                  <input
                    value={quickSearch}
                    onChange={e => setQuickSearch(e.target.value)}
                    placeholder="جستجوی نماد یا نام شرکت در کل دیتاست..."
                    className="global-search-input w-60 md:w-80 px-3 py-2 text-xs rounded bg-[var(--bg-canvas)] border border-[var(--border-subtle)] focus:outline-none focus:border-blue-500"
                  />
                  {searchResults.length > 0 && (
                    <div className="global-search-results absolute right-0 mt-2 w-full rounded border border-[var(--border-subtle)] bg-[var(--bg-panel)] shadow-xl z-50 text-xs max-h-72 overflow-y-auto">
                      {searchResults.map(m => (
                        <button key={m.symbol} onClick={() => openStock(m.symbol)} className="global-search-result w-full text-right p-2 hover:bg-[var(--bg-elevated)] flex justify-between gap-2">
                          <span className="font-bold text-blue-400">{m.symbol}</span>
                          <span className="text-[var(--text-secondary)] truncate">{m.company_name || m.market}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              <div className="app-toolbar flex items-center gap-2 text-xs">
                <button onClick={toggleDateMode} className="toolbar-btn px-3 py-1.5 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)]" title="همهٔ تاریخ‌ها (نمودارها و جدول‌ها) بین شمسی و میلادی جابه‌جا می‌شوند">
                  تاریخ: {dateMode === 'jalali' ? 'شمسی' : 'میلادی'}
                </button>
                <button onClick={() => setCurrency(c => c === 'IRR' ? 'IRT' : 'IRR')} className="toolbar-btn px-3 py-1.5 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)]">
                  واحد: {currency === 'IRR' ? 'ریال' : 'تومان'}
                </button>
                <button onClick={() => setTheme(t => t === 'dark' ? 'light' : 'dark')} className="toolbar-btn px-3 py-1.5 rounded border border-[var(--border-subtle)] bg-[var(--bg-elevated)]">
                  {theme === 'dark' ? 'تم روشن' : 'تم تیره'}
                </button>
              </div>
            </div>

            <nav className="app-nav max-w-[1600px] mx-auto px-3 flex items-center gap-1 overflow-x-auto border-t border-[var(--border-subtle)] text-xs" aria-label="ناوبری اصلی">
              {NAV_PAGES.map(n => (
                <button key={n.id} onClick={() => setPage(n.id)} aria-current={page === n.id ? 'page' : undefined} className={`nav-item px-3 py-2 whitespace-nowrap border-b-2 transition-colors ${page === n.id ? 'is-active border-blue-500 text-blue-400 font-bold' : 'border-transparent text-[var(--text-secondary)] hover:text-[var(--text-primary)]'}`}>
                  {n.label}
                </button>
              ))}
            </nav>
          </header>

          <main className="app-main flex-1 max-w-[1600px] w-full mx-auto p-4 md:p-5">
            {page === 'market' && <MarketDashboardPage onOpenStock={openStock} onOpenInvestor={openInvestor} currency={currency} dateMode={dateMode} />}
            {page === 'stock' && <StockDetailPage symbol={activeSymbol} currency={currency} theme={theme} dateMode={dateMode} onOpenInvestor={openInvestor} />}
            {page === 'shareholders' && <ShareholderIntelligencePage currency={currency} theme={theme} onOpenStock={openStock} onOpenInvestor={openInvestor} dateMode={dateMode} />}
            {page === 'portfolio' && <PortfolioReconstructionPage currency={currency} theme={theme} onOpenStock={openStock} dateMode={dateMode} initialInvestor={portfolioInvestor} />}
            {page === 'screener' && <ScreenerPage onOpenStock={openStock} />}
            {page === 'watchlist' && <WatchlistPage onOpenStock={openStock} currency={currency} />}
            {page === 'account' && <AccountAndPlansPage plan={plan} setPlan={setPlan} />}
            {page === 'admin' && <AdminDashboardPage />}
          </main>
        </div>
      );
    }

    ReactDOM.createRoot(document.getElementById('root')).render(<App />);

import React, { useState, useEffect, useRef } from 'react';
import { Routes, Route, NavLink, Navigate, useLocation, useNavigate } from 'react-router-dom';
import styles from './App.module.css';
import { ROUTES } from './routes';
import { getAdminSettings } from './api/client';
import {
  SettingsIcon, SearchIcon, XIcon, SunIcon, MoonIcon, GithubIcon,
} from './components/Icons';
import HealthChipGroup from './components/HealthChipGroup';
import SearchContext from './contexts/SearchContext';
import { ConnectionProvider, useConnection } from './contexts/ConnectionContext';
import pkg from '../package.json';

// UI-only kind ordering/routing for the health chips — where each chip's
// "settings" click should land. The health-poll's own kind mapping lives in
// ConnectionContext.
const HEALTH_KINDS = [
  { kind: 'cdr', settingsHash: '#cdr-connections' },
  { kind: 'mcs', settingsHash: '#mcs-connections' },
];

function MenuIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
      <path d="M3 5h12M3 9h12M3 13h12" />
    </svg>
  );
}

function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const [navOpen, setNavOpen] = useState(false);
  const { cdr } = useConnection();
  const [theme, setTheme] = useState(() => {
    const current = localStorage.getItem('lenny-theme');
    if (current) return current;
    const legacy = localStorage.getItem('mct2-theme');
    if (legacy) {
      localStorage.setItem('lenny-theme', legacy);
      localStorage.removeItem('mct2-theme');
      return legacy;
    }
    return 'light';
  });
  const [query, setQuery] = useState('');
  const [features, setFeatures] = useState({ validation: false });
  const searchRef = useRef(null);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    localStorage.setItem('lenny-theme', theme);
  }, [theme]);

  useEffect(() => {
    setQuery('');
    setNavOpen(false);
  }, [location.pathname]);

  // Health-poll logic (chips, failure debounce, visibility-aware interval)
  // lives in ConnectionProvider — see contexts/ConnectionContext.js (#396).

  useEffect(() => {
    getAdminSettings()
      .then(s => setFeatures({ validation: s.validation_enabled ?? false }))
      .catch(() => {});
    const h = (e) => setFeatures({ validation: e.detail.validation_enabled ?? false });
    window.addEventListener('admin-settings-changed', h);
    return () => window.removeEventListener('admin-settings-changed', h);
  }, []);

  useEffect(() => {
    const h = (e) => {
      const active = document.activeElement;
      const isInput = active.tagName === 'INPUT' || active.tagName === 'SELECT' || active.tagName === 'TEXTAREA' || active.isContentEditable;

      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        searchRef.current?.focus();
        return;
      }
      if (e.key === 'Escape' && document.activeElement === searchRef.current) {
        searchRef.current.blur();
        setQuery('');
        return;
      }
      if (e.key === 'Escape') {
        setNavOpen(false);
        return;
      }
      if (!isInput && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) {
        // Feature-filtered inside the effect (not the render body) so this
        // array's identity doesn't change every render — [navigate, features]
        // stays the full dependency list and the listener isn't re-subscribed
        // on every keystroke.
        const shortcutRoutes = ROUTES.filter(r => r.nav && (!r.feature || features[r.feature]));
        const match = shortcutRoutes.find(r => r.nav.kbd.toLowerCase() === e.key.toLowerCase());
        if (match) navigate(match.path);
      }
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [navigate, features]);

  const navItems = ROUTES.filter(r => r.nav && (!r.feature || features[r.feature]));
  const basePath = '/' + location.pathname.split('/')[1];
  const activeRoute = ROUTES.find(r => r.path === basePath);
  const pageTitle = activeRoute?.title || 'Lenny';
  const searchPlaceholder = activeRoute?.searchPlaceholder || 'Search…';
  const cdrChip = cdr;
  const cdrOk = cdrChip.state === 'healthy';

  return (
    <SearchContext.Provider value={{ query, setQuery }}>
      <div className={`${styles.screen} ${navOpen ? styles.navOpen : ''}`}>
        <button
          className={styles.navBackdrop}
          type="button"
          aria-label="Close navigation"
          onClick={() => setNavOpen(false)}
        />
        {/* Brand */}
        <div className={styles.brand}>
          <div className={styles.brandMark}>L</div>
          <span className={styles.brandName}>Lenny</span>
        </div>

        {/* Topbar */}
        <header className={styles.topbar}>
          <button
            className={styles.hamburger}
            type="button"
            aria-label="Open navigation"
            aria-expanded={navOpen}
            onClick={() => setNavOpen(true)}
          >
            <MenuIcon />
          </button>
          <span className={styles.crumb}>{pageTitle}</span>
          <div className={styles.spacer} />
          <div className={styles.topbarRight}>
            <HealthChipGroup
              kinds={HEALTH_KINDS}
              onChipClick={(hash) => navigate(`/settings${hash}`)}
            />
            <div className={styles.searchWrap}>
              <SearchIcon className={styles.searchIcon} />
              <input
                ref={searchRef}
                className={styles.searchInput}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={searchPlaceholder}
                aria-label="Search"
              />
              {query
                ? <button className={styles.searchClear} onClick={() => setQuery('')} aria-label="Clear search"><XIcon /></button>
                : <kbd className={styles.kbdInline}>⌘K</kbd>
              }
            </div>
            <button
              className={styles.themeBtn}
              onClick={() => setTheme(t => t === 'light' ? 'dark' : 'light')}
              aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
              title={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
            >
              {theme === 'light' ? <MoonIcon /> : <SunIcon />}
            </button>
          </div>
        </header>

        {/* Sidebar nav */}
        <nav className={styles.nav} aria-label="Main navigation">
          <button
            className={styles.navClose}
            type="button"
            aria-label="Close navigation"
            onClick={() => setNavOpen(false)}
          >
            <XIcon />
            <span>Close</span>
          </button>
          <div className={styles.navGroupLabel}>Workspace</div>
          {navItems.map(({ path, nav: { label, Icon, kbd } }) => (
            <NavLink
              key={path}
              to={path}
              className={({ isActive }) => `${styles.navItem} ${isActive ? styles.navItemActive : ''}`}
            >
              <Icon className={styles.navIcon} />
              <span className={styles.navLabel}>{label}</span>
              <span className={styles.navKbd}>{kbd}</span>
            </NavLink>
          ))}

          <div className={styles.navGroupLabel} style={{ marginTop: 16 }}>Data source</div>
          <div className={styles.dataSourceItem}>
            <span className={styles.navIcon}>
              <span className={`${styles.smallDot} ${cdrOk ? styles.smallDotOk : styles.smallDotErr}`} />
            </span>
            <span className={styles.navLabel}>{cdrChip.name || 'Local CDR'}</span>
          </div>

          <NavLink
            to="/settings"
            className={({ isActive }) => `${styles.navItem} ${styles.navItemSettings} ${isActive ? styles.navItemActive : ''}`}
          >
            <SettingsIcon className={styles.navIcon} />
            <span className={styles.navLabel}>Settings</span>
          </NavLink>

          <div className={styles.statusFooter}>
            <div
              className={styles.statusRow}
              title={!cdrOk && cdrChip.errorDetails?.hint ? cdrChip.errorDetails.hint : undefined}
            >
              <span className={`${styles.statusDot} ${cdrOk ? styles.statusDotOk : ''}`} />
              {cdrOk ? 'All services healthy' : 'CDR unavailable'}
            </div>
            <div className={styles.statusVersion}>Lenny · v{pkg.version}</div>
            <a
              className={styles.repoLink}
              href="https://github.com/Bellese/Lenny"
              target="_blank"
              rel="noopener noreferrer"
              aria-label="View Lenny source on GitHub (opens in new tab)"
            >
              <GithubIcon className={styles.repoLinkIcon} />
              <span>github.com/Bellese/Lenny</span>
            </a>
          </div>
        </nav>

        {/* Main content */}
        <main className={styles.main} role="main">
          <Routes>
            {ROUTES.map(({ path, redirectTo, Component }) => (
              <Route
                key={path}
                path={path}
                element={redirectTo ? <Navigate to={redirectTo} replace /> : <Component />}
              />
            ))}
          </Routes>
        </main>
      </div>
    </SearchContext.Provider>
  );
}

// Split from AppShell because a component can't consume a value from a
// Provider it renders itself — AppShell needs `cdr` (sidebar, status
// footer), so ConnectionProvider has to sit one level above it. Do not
// collapse this back into a single component.
export default function App() {
  return (
    <ConnectionProvider>
      <AppShell />
    </ConnectionProvider>
  );
}

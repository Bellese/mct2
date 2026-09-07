import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from 'react';
import { getHealth } from '../api/client';

// Health-payload kind -> response key. This is the health-poll's own concern
// (mapping GET /health sections to internal chip kinds) and is separate from
// any UI-only kind ordering/routing that lives in the consuming components.
const HEALTH_KINDS = [
  { kind: 'cdr', healthKey: 'cdr' },
  { kind: 'mcs', healthKey: 'measure_engine' },
];

// Debounce: only flip to 'unreachable' after this many consecutive failed probes.
const FAILURE_DEBOUNCE = 2;

const DEFAULT_VALUE = {
  cdr: { id: null, name: '', state: 'pending', isReadOnly: false, errorDetails: null },
  mcs: { id: null, name: '', state: 'pending', isReadOnly: false, errorDetails: null },
  refresh: () => {},
};

const ConnectionContext = createContext(DEFAULT_VALUE);

export default ConnectionContext;

export function useConnection() {
  return useContext(ConnectionContext);
}

export function ConnectionProvider({ children }) {
  // Per-kind chip state: { cdr: {...}, mcs: {...} }. Each entry carries
  // everything downstream consumers (HealthChipGroup, MeasuresPage,
  // JobsPage, App's sidebar) need: state, name, id, isReadOnly, errorDetails.
  const [chips, setChips] = useState({
    cdr: { state: 'pending', name: '', id: null, isReadOnly: false, errorDetails: null },
    mcs: { state: 'pending', name: '', id: null, isReadOnly: false, errorDetails: null },
  });
  const failureCounts = useRef({ cdr: 0, mcs: 0 });

  // Multi-kind health probe — lifted from App.js essentially as-is (#396).
  const checkHealth = useCallback(async () => {
    let health;
    try {
      health = await getHealth();
    } catch {
      // Network error — bump failure counts for both kinds. Fail closed on
      // isReadOnly: a network-level failure tells us nothing new about
      // read-only status, so carry forward whatever we last confirmed
      // rather than resetting to false and re-enabling destructive controls
      // against a server we previously knew was read-only (#396).
      setChips(prev => {
        const next = {};
        for (const { kind } of HEALTH_KINDS) {
          failureCounts.current[kind] = failureCounts.current[kind] + 1;
          const nextState = failureCounts.current[kind] >= FAILURE_DEBOUNCE ? 'unreachable' : 'pending';
          next[kind] = {
            state: nextState,
            name: '',
            isReadOnly: prev[kind]?.isReadOnly ?? false,
            errorDetails: null,
            id: null,
          };
        }
        return { ...prev, ...next };
      });
      return;
    }

    setChips(prev => {
      const next = {};
      for (const { kind, healthKey } of HEALTH_KINDS) {
        const rawSection = health?.[healthKey];
        // Only trust is_read_only from a response that actually came back
        // with this kind's block populated — a transient/degraded probe
        // that omits it must not flip a previously-known read-only
        // connection back to writable (#396).
        const hasSection = rawSection != null;
        const section = rawSection || {};
        const ok = section.status === 'connected' || section.status === 'healthy';
        const isReadOnly = hasSection ? !!section.is_read_only : (prev[kind]?.isReadOnly ?? false);
        // Both kinds carry a connection id: consumers key effects on it so a
        // switch of either the MCS or the CDR refetches the lists derived from
        // it (#396 for mcs, #404 for cdr).
        const idField = { id: section.id ?? null };
        if (ok) {
          failureCounts.current[kind] = 0;
          next[kind] = {
            state: 'healthy',
            name: section.name || '',
            isReadOnly,
            errorDetails: null,
            ...idField,
          };
        } else {
          failureCounts.current[kind] = failureCounts.current[kind] + 1;
          const debounced = failureCounts.current[kind] >= FAILURE_DEBOUNCE;
          next[kind] = {
            state: debounced ? 'unreachable' : 'pending',
            name: section.name || '',
            isReadOnly,
            errorDetails: section.error_details || null,
            ...idField,
          };
        }
      }
      return { ...prev, ...next };
    });
  }, []);

  useEffect(() => {
    let interval = null;
    const start = () => {
      if (interval !== null) return;
      checkHealth();
      interval = setInterval(checkHealth, 30000);
    };
    const stop = () => {
      if (interval === null) return;
      clearInterval(interval);
      interval = null;
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') start();
      else stop();
    };
    if (document.visibilityState === 'visible') start();
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [checkHealth]);

  const value = {
    cdr: chips.cdr,
    mcs: chips.mcs,
    refresh: checkHealth,
  };

  return (
    <ConnectionContext.Provider value={value}>
      {children}
    </ConnectionContext.Provider>
  );
}

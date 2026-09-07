import React, { useEffect, useState, useCallback } from 'react';
import { getPatientGroups } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import { useSearch } from '../contexts/SearchContext';
import { useConnection } from '../contexts/ConnectionContext';
import styles from './PatientsPage.module.css';

// How many patients a cohort holds is not a single field. A FHIR Group may
// enumerate `member`, or state `quantity` and enumerate nothing (a
// characteristic-based cohort), or say neither. Reporting an honest "—" beats
// a confident "0" on somebody's real cohort (#404).
export function memberCountLabel(group) {
  if (group.member_count) return String(group.member_count);
  if (group.quantity != null) return String(group.quantity);
  if (group.member_count === 0 && group.quantity === 0) return '0';
  return '—';
}

export default function PatientsPage() {
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const { query } = useSearch();
  const { cdr } = useConnection();

  const loadGroups = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getPatientGroups();
      setGroups(data.groups || []);
    } catch (err) {
      // Never leave the previous CDR's cohorts on screen — showing another
      // server's cohorts as if they were this one's is the exact failure this
      // module exists to prevent.
      setGroups([]);
      setError(err.message || 'Failed to load patient groups');
    } finally {
      setLoading(false);
    }
  }, []);

  // Re-fetch whenever the active CDR changes — the whole point of the module
  // is that it describes the server you are actually pointed at (#404).
  useEffect(() => { loadGroups(); }, [loadGroups, cdr.id]);

  const q = query.trim().toLowerCase();
  const visible = groups.filter(g => {
    if (!q) return true;
    return (g.name || '').toLowerCase().includes(q) || (g.id || '').toLowerCase().includes(q);
  });

  const cdrLabel = cdr.name || 'the active CDR';

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <div className={styles.eyebrow}>Cohorts</div>
          <h1 className={styles.title}>Patients</h1>
          {!loading && !error && visible.length > 0 && (
            <div className={styles.subtitle}>
              {visible.length} {visible.length === 1 ? 'Group' : 'Groups'} on {cdrLabel}
            </div>
          )}
        </div>
        <button
          type="button"
          className={styles.refreshBtn}
          onClick={loadGroups}
          disabled={loading}
        >
          Refresh
        </button>
      </div>

      {!loading && error && (
        <div className={styles.errorState}>
          <ErrorBanner title={`${cdrLabel} is unreachable`} message={error} />
          <button type="button" className={styles.refreshBtn} onClick={loadGroups}>Retry</button>
        </div>
      )}

      {!loading && !error && groups.length === 0 && (
        <div className={styles.empty}>
          <p className={styles.emptyTitle}>No patient Groups on {cdrLabel}.</p>
          <p>
            The read succeeded — this server simply has none. Group resources are
            optional in FHIR, so this is a normal state for a CDR that has never had
            cohorts defined on it.
          </p>
          <p>
            Measures still run: choose <strong>All patients</strong> under
            Jobs → New calculation. To scope a calculation to a cohort instead,
            load a bundle that contains a Group resource.
          </p>
        </div>
      )}

      {!loading && !error && groups.length > 0 && (
        <div className={styles.tableScroll}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Name</th>
                <th>ID</th>
                <th>Type</th>
                <th className={styles.numeric}>Members</th>
              </tr>
            </thead>
            <tbody>
              {visible.map(g => (
                <tr key={g.id} data-testid={`group-row-${g.id}`}>
                  <td className={styles.groupName}>{g.name || g.id}</td>
                  <td><code className={styles.idChip}>{g.id}</code></td>
                  <td>{g.type || '—'}</td>
                  <td className={styles.numeric} data-testid={`member-count-${g.id}`}>
                    {memberCountLabel(g)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

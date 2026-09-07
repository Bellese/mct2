import React, { useState, useEffect, useLayoutEffect, useCallback, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import styles from './ResultsPage.module.css';
import {
  getJobs, getResults, createJob, getMeasureReportBundle,
  getJobComparison, getAdminSettings, getEvaluatedResources,
} from '../api/client';
import { useToast } from '../components/Toast';
import PopulationDots from '../components/PopulationDots';
import DistBar from '../components/DistBar';
import { CheckIcon, WarnIcon } from '../components/Icons';
import { useSearch } from '../contexts/SearchContext';
import { extractCmsId, cleanMeasureName, measureOptionLabel, measureDisplayLabel } from '../utils/measureFormat';
import { formatDateTime, formatDuration } from '../utils/dateFormat';

function timeAgo(dateStr) {
  if (!dateStr) return null;
  const diff = Math.floor((Date.now() - new Date(dateStr)) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function exportCsv(patients, measureId, measureName, period) {
  const header = ['Patient ID', 'Name', 'Initial Population', 'Denominator', 'Numerator', 'Denom Exclusion', 'Numer Exclusion'];
  const rows = patients.map(p => [
    p.patient_id || p.id || '',
    p.patient_name || p.name || '',
    p.populations?.initial_population ? 'Yes' : 'No',
    p.populations?.denominator ? 'Yes' : 'No',
    p.populations?.numerator ? 'Yes' : 'No',
    p.populations?.denominator_exclusion ? 'Yes' : 'No',
    p.populations?.numerator_exclusion ? 'Yes' : 'No',
  ]);
  const csv = [header, ...rows].map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const cmsId = extractCmsId(measureId);
  const fileLabel = cmsId ? `${cmsId}-${cleanMeasureName(measureName || '')}` : (measureName || 'measure');
  a.download = `results-${fileLabel}-${period || 'period'}.csv`.replace(/\s+/g, '-');
  a.click();
  URL.revokeObjectURL(url);
}

/**
 * Map server-side population shape to normalized PopulationCounts.
 * Handles both underscore keys (from /results) and hyphen keys (from /jobs/:id/comparison).
 * @returns {{ ip: 0|1, den: 0|1, denExc: 0|1, num: 0|1, numExc: 0|1 }}
 */
function toCounts(populations) {
  if (!populations) return { ip: 0, den: 0, denExc: 0, num: 0, numExc: 0 };
  return {
    ip:     (populations.initial_population     || populations['initial-population'])     ? 1 : 0,
    den:    populations.denominator                                                       ? 1 : 0,
    denExc: (populations.denominator_exclusion  || populations['denominator-exclusion'])  ? 1 : 0,
    num:    populations.numerator                                                         ? 1 : 0,
    numExc: (populations.numerator_exclusion    || populations['numerator-exclusion'])    ? 1 : 0,
  };
}

const POP_BREAKDOWN = [
  { key: 'ip',     label: 'Initial Population',    apiKey: 'initial-population' },
  { key: 'den',    label: 'Denominator',           apiKey: 'denominator' },
  { key: 'denExc', label: 'Denominator Exclusion', apiKey: 'denominator-exclusion' },
  { key: 'num',    label: 'Numerator',             apiKey: 'numerator' },
  { key: 'numExc', label: 'Numerator Exclusion',   apiKey: 'numerator-exclusion' },
];

function isFailure(row) {
  return row.error !== null || row.match === false;
}

function translateResource(resource) {
  if (!resource) return null;
  const type = resource.resourceType;
  if (type === 'Observation') {
    const code = resource.code?.coding?.[0]?.display || resource.code?.text || 'Observation';
    const value = resource.valueQuantity
      ? `${resource.valueQuantity.value}${resource.valueQuantity.unit ? ' ' + resource.valueQuantity.unit : ''}`
      : resource.valueCodeableConcept?.text || resource.valueString || '';
    const date = resource.effectiveDateTime || resource.issued || '';
    const dateStr = date ? ` (${new Date(date).toLocaleDateString()})` : '';
    return { label: code, value: `${value}${dateStr}` };
  }
  if (type === 'Condition') {
    const name = resource.code?.coding?.[0]?.display || resource.code?.text || 'Condition';
    const status = resource.clinicalStatus?.coding?.[0]?.code || '';
    const onset = resource.onsetDateTime || '';
    const onsetStr = onset ? ` since ${new Date(onset).toLocaleDateString()}` : '';
    return { label: 'Diagnosis', value: `${name} (${status}${onsetStr})` };
  }
  if (type === 'Procedure') {
    const name = resource.code?.coding?.[0]?.display || resource.code?.text || 'Procedure';
    const date = resource.performedDateTime || resource.performedPeriod?.start || '';
    const dateStr = date ? ` (${new Date(date).toLocaleDateString()})` : '';
    return { label: 'Procedure', value: `${name}${dateStr}` };
  }
  if (type === 'Encounter') {
    const encType = resource.type?.[0]?.coding?.[0]?.display || resource.type?.[0]?.text || 'Encounter';
    const date = resource.period?.start || '';
    const dateStr = date ? ` (${new Date(date).toLocaleDateString()})` : '';
    return { label: 'Encounter', value: `${encType}${dateStr}` };
  }
  if (type === 'MedicationRequest') {
    const med = resource.medicationCodeableConcept?.coding?.[0]?.display || resource.medicationCodeableConcept?.text || 'Medication';
    const date = resource.authoredOn || '';
    const dateStr = date ? ` (${new Date(date).toLocaleDateString()})` : '';
    return { label: 'Medication', value: `${med}${dateStr}` };
  }
  const display = resource.code?.coding?.[0]?.display || resource.code?.text || type || 'Resource';
  return { label: type || 'Resource', value: display };
}

// ─── Sub-components ──────────────────────────────────────────────────────────

function TruncatedId({ id }) {
  const [copied, setCopied] = useState(false);
  if (!id) return <span className={styles.mono} style={{ fontSize: 12 }}>—</span>;
  const short = id.length > 14 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id;
  const copy = (e) => {
    e.stopPropagation();
    navigator.clipboard.writeText(id).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };
  return (
    <button className={styles.idCopyBtn} title={id} onClick={copy} type="button">
      {copied ? 'copied' : short}
    </button>
  );
}

function CopyIdButton({ id }) {
  const [copied, setCopied] = useState(false);
  if (!id) return null;
  const copy = (e) => {
    e.stopPropagation();
    navigator.clipboard.writeText(id).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };
  return (
    <button className={styles.copyIconBtn} title="Copy FHIR ID" onClick={copy} type="button" aria-label="Copy FHIR ID">
      {copied
        ? <span style={{ fontSize: 10, color: 'var(--accent)' }}>✓</span>
        : <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
            <rect x="4" y="4" width="7" height="7" rx="1.2" />
            <path d="M8 4V2.5A1.5 1.5 0 006.5 1H2.5A1.5 1.5 0 001 2.5v4A1.5 1.5 0 002.5 8H4" />
          </svg>
      }
    </button>
  );
}

function StatusPill({ kind, children, title }) {
  const cls = kind === 'match' ? styles.pillMatch
    : kind === 'miss'  ? styles.pillMiss
    : kind === 'error' ? styles.pillError
    : styles.pillNeutral;
  return (
    <span className={`${styles.pill} ${cls}`} title={title} style={{ cursor: title ? 'help' : undefined }}>
      {children}
    </span>
  );
}

function FilterChip({ chipId, label, count, detail, active, danger, onClick }) {
  const cls = [
    styles.chip,
    active && danger ? styles.chipDangerActive : active ? styles.chipActive : '',
  ].filter(Boolean).join(' ');
  return (
    <button className={cls} onClick={() => onClick(chipId)} aria-pressed={active}>
      {label}
      {count != null && <span className={styles.chipCount}>{count}</span>}
      {detail && <span className={styles.chipDetail}>{detail}</span>}
    </button>
  );
}

function PopDesc({ text }) {
  const [expanded, setExpanded] = useState(false);
  const [isClamped, setIsClamped] = useState(false);
  const ref = useRef(null);

  useLayoutEffect(() => {
    if (ref.current) {
      setIsClamped(ref.current.scrollHeight > ref.current.clientHeight);
    }
  }, [text]);

  if (!text) return null;
  return (
    <span className={styles.popDescWrap}>
      <button
        ref={ref}
        className={`${styles.popDesc} ${expanded ? styles.popDescExpanded : ''}`}
        onClick={isClamped || expanded ? e => { e.stopPropagation(); setExpanded(v => !v); } : undefined}
        type="button"
        title={expanded ? undefined : text}
        style={isClamped || expanded ? undefined : { cursor: 'default', pointerEvents: 'none' }}
      >
        {text}
      </button>
      {!expanded && isClamped && (
        <span className={styles.popDescMore} aria-hidden="true">more ▾</span>
      )}
    </span>
  );
}

function ExpandedPanel({ row, expectedLoaded, colSpan, popDescriptions, definedPops }) {
  const [resources, setResources] = useState(null);
  const [resLoading, setResLoading] = useState(true);
  const [resError, setResError] = useState(null);
  const [showRawFhir, setShowRawFhir] = useState(false);
  const [expandedRaw, setExpandedRaw] = useState({});

  useEffect(() => {
    if (!row.resultId) return;
    setResLoading(true);
    setResError(null);
    getEvaluatedResources(row.resultId)
      .then(data => setResources(data))
      .catch(err => setResError(err.message || 'Failed to load'))
      .finally(() => setResLoading(false));
  }, [row.resultId]);

  const verdictPill = row.error
    ? <StatusPill kind="error">{row.error}</StatusPill>
    : !expectedLoaded
      ? null
      : row.match
        ? <StatusPill kind="match">match</StatusPill>
        : <StatusPill kind="miss">mismatch</StatusPill>;

  const resourceList = Array.isArray(resources) ? resources : (resources?.resources || []);

  return (
    <tr className={styles.expandedRow}>
      <td colSpan={colSpan}>
        <div className={styles.expandedInner}>
          {/* Left: population breakdown */}
          <div className={styles.miniCard}>
            <div className={styles.miniHead}>
              <span>Population breakdown</span>
              {verdictPill}
            </div>
            <table className={styles.miniTable}>
              <thead>
                <tr>
                  <th>Population</th>
                  <th className={styles.numCell}>Actual</th>
                  {expectedLoaded && <th className={styles.numCell}>Expected</th>}
                  {expectedLoaded && <th className={styles.numCell}></th>}
                </tr>
              </thead>
              <tbody>
                {POP_BREAKDOWN.filter(({ apiKey }) =>
                  !definedPops || definedPops.includes(apiKey)
                ).map(({ key, label, apiKey }) => {
                  const a = row.act[key] ?? 0;
                  const e = row.exp != null ? (row.exp[key] ?? 0) : null;
                  const ok = !expectedLoaded || row.error || a === e;
                  const desc = popDescriptions?.[apiKey] || null;
                  return (
                    <tr key={key} className={!ok ? styles.miniRowMiss : ''}>
                      <td>
                        <div>{label}</div>
                        <PopDesc text={desc} />
                      </td>
                      <td className={`${styles.numCell} ${!ok ? styles.numCellMiss : ''}`}>{a}</td>
                      {expectedLoaded && (
                        <td className={styles.numCell}>{row.error ? '—' : (e ?? '—')}</td>
                      )}
                      {expectedLoaded && (
                        <td className={styles.numCell}>
                          {!row.error && (ok
                            ? <CheckIcon className={styles.iconOk} />
                            : <WarnIcon className={styles.iconErr} />
                          )}
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Right: patient context + evaluated resources */}
          <div className={styles.miniCard}>
            <div className={styles.miniHead}>
              <span>Patient context</span>
            </div>
            <div className={styles.kvBody}>
              <div className={styles.kvRow}>
                <span className={styles.kvKey}>Patient</span>
                <b>{row.name || '—'}</b>
              </div>
              <div className={styles.kvRow}>
                <span className={styles.kvKey}>Patient ID</span>
                <span className={styles.kvIdRow}>
                  <span className={styles.mono} style={{ fontSize: 12 }}>{row.patientId || '—'}</span>
                  {row.patientId && <CopyIdButton id={row.patientId} />}
                </span>
              </div>
              {row.error && (
                <div className={styles.kvRow}>
                  <span className={styles.kvKey}>Error</span>
                  <span className={styles.errorText}>{row.errorMessage || row.error}</span>
                </div>
              )}
            </div>

            {/* Evaluated resources */}
            <div className={styles.resSection}>
              <div className={styles.resSectionHead}>
                <span>Evaluated resources</span>
                {!resLoading && !resError && resourceList.length > 0 && (
                  <button
                    className={styles.rawFhirToggle}
                    onClick={() => setShowRawFhir(v => !v)}
                    type="button"
                  >
                    {showRawFhir ? 'Clinical view' : 'Raw FHIR'}
                  </button>
                )}
              </div>

              {resLoading && (
                <div className={styles.resList}>
                  {[1, 2, 3].map(i => <div key={i} className={styles.resSkeleton} />)}
                </div>
              )}

              {resError && (
                <div className={styles.resEmpty}>Clinical data unavailable.</div>
              )}

              {!resLoading && !resError && !showRawFhir && (
                <div className={styles.resList}>
                  {resourceList.length === 0
                    ? <div className={styles.resEmpty}>No evaluated resources.</div>
                    : resourceList.map((res, i) => {
                        const t = translateResource(res);
                        if (!t) return null;
                        return (
                          <div key={i} className={styles.resRow}>
                            <span className={styles.resLabel}>{t.label}</span>
                            <span className={styles.resValue}>{t.value}</span>
                          </div>
                        );
                      })
                  }
                </div>
              )}

              {!resLoading && !resError && showRawFhir && (
                <div className={styles.resList}>
                  {resourceList.map((res, i) => (
                    <div key={i} className={styles.rawBlock}>
                      <button
                        className={styles.rawToggle}
                        onClick={() => setExpandedRaw(p => ({ ...p, [i]: !p[i] }))}
                        type="button"
                      >
                        <span>{expandedRaw[i] ? '▾' : '▸'}</span>
                        <span>{res.resourceType || 'Resource'}{res.id ? `/${res.id}` : ''}</span>
                      </button>
                      {expandedRaw[i] && (
                        <pre className={styles.rawJson}>{JSON.stringify(res, null, 2)}</pre>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </td>
    </tr>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function ResultsPage() {
  const { jobId: routeJobId } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const { query, setQuery } = useSearch();

  const [jobs, setJobs] = useState([]);
  const [selectedJobId, setSelectedJobId] = useState(routeJobId || '');
  const [results, setResults] = useState(null);
  const [comparisonData, setComparisonData] = useState(null);
  const [adminSettings, setAdminSettings] = useState({ comparison_enabled: false, validation_enabled: false });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expandedId, setExpandedId] = useState(null);
  const [filter, setFilter] = useState('all');
  const [rerunning, setRerunning] = useState(false);
  const [bundleDownloading, setBundleDownloading] = useState(false);

  const filterInitRef = useRef(null);

  // Admin settings
  useEffect(() => {
    getAdminSettings().then(setAdminSettings).catch(() => {});
    const h = (e) => setAdminSettings(e.detail);
    window.addEventListener('admin-settings-changed', h);
    return () => window.removeEventListener('admin-settings-changed', h);
  }, []);

  // Jobs list
  useEffect(() => {
    async function loadJobs() {
      try {
        const data = await getJobs();
        const all = Array.isArray(data) ? data : data.jobs || [];
        const completed = all.filter(j => {
          const s = (j.status || '').toLowerCase();
          return s === 'completed' || s === 'complete';
        });
        setJobs(completed);
        const preferredId = routeJobId || selectedJobId;
        const preferredExists = preferredId && completed.some(j => String(j.id) === String(preferredId));
        if (preferredExists) {
          if (String(selectedJobId) !== String(preferredId)) setSelectedJobId(String(preferredId));
          return;
        }
        const fallbackId = completed[0] ? String(completed[0].id) : '';
        if (String(selectedJobId) !== fallbackId) setSelectedJobId(fallbackId);
        if (routeJobId && String(routeJobId) !== fallbackId) {
          navigate(fallbackId ? `/results/${fallbackId}` : '/results', { replace: true });
        }
      } catch { /* non-blocking */ }
    }
    loadJobs();
  }, [navigate, routeJobId, selectedJobId]);

  // Results + comparison
  const loadResults = useCallback(async () => {
    if (!selectedJobId) { setResults(null); setComparisonData(null); setLoading(false); return; }
    setLoading(true);
    setError(null);
    setExpandedId(null);
    try {
      const promises = [getResults(selectedJobId)];
      if (adminSettings.comparison_enabled) {
        promises.push(getJobComparison(selectedJobId).catch(() => null));
      }
      const [resultsData, cmpData = null] = await Promise.all(promises);
      setResults(resultsData);
      setComparisonData(adminSettings.comparison_enabled ? cmpData : null);
    } catch (err) {
      setError(err.message || 'Error loading results');
    } finally {
      setLoading(false);
    }
  }, [selectedJobId, adminSettings.comparison_enabled]);

  useEffect(() => { loadResults(); }, [loadResults]);

  // Derived: expectedLoaded
  const expectedLoaded = adminSettings.comparison_enabled && !!comparisonData?.has_expected;


  const handleJobChange = (e) => {
    const id = e.target.value;
    setSelectedJobId(id);
    navigate(`/results/${id}`, { replace: true });
  };

  const handleRerun = async () => {
    if (!selectedJob) return;
    setRerunning(true);
    try {
      await createJob({
        measure_id: selectedJob.measure_id,
        measure_name: selectedJob.measure_name,
        group_id: selectedJob.group_id || undefined,
        period_start: selectedJob.period_start || undefined,
        period_end: selectedJob.period_end || undefined,
        // Carry the original job's submission workflow. Omitting it let the
        // backend default (direct_load) win, so re-running a DEQM job silently
        // produced a direct-load job -- same measure, same period, different
        // delivery path, with nothing on screen saying so.
        workflow: selectedJob.workflow || undefined,
      });
      toast.success('Re-run started — check the Jobs page');
      navigate('/jobs');
    } catch (err) {
      toast.error(`Failed to start re-run: ${err.message}`);
    } finally {
      setRerunning(false);
    }
  };

  const handleDownloadBundle = async () => {
    if (!selectedJobId) return;
    setBundleDownloading(true);
    try {
      const bundle = await getMeasureReportBundle(selectedJobId);
      const json = JSON.stringify(bundle, null, 2);
      const blob = new Blob([json], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      const mid = (selectedJob?.measure_id || 'measure').replace(/[^a-zA-Z0-9-_]/g, '-');
      const periodStr = selectedJob?.period_start && selectedJob?.period_end
        ? `${selectedJob.period_start}-to-${selectedJob.period_end}`
        : 'unknown-period';
      a.download = `measure-report-${mid}-${periodStr}-job-${selectedJobId}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast.error(`Failed to download FHIR JSON: ${err.message}`);
    } finally {
      setBundleDownloading(false);
    }
  };

  // ─── Derived data ───────────────────────────────────────────────────────────

  const selectedJob = jobs.find(j => String(j.id) === String(selectedJobId));
  const populations = results?.populations || {};
  const ip  = populations.initial_population;
  const den = populations.denominator;
  const num = populations.numerator;
  const exc = populations.denominator_exclusion;
  const perfRate = results?.performance_rate;
  const measureName = results?.measure_name || selectedJob?.measure_name || selectedJob?.measure_id || '';
  const period = selectedJob?.period_start && selectedJob?.period_end
    ? `${selectedJob.period_start} → ${selectedJob.period_end}` : '';
  const completedAgo = selectedJob?.completed_at ? timeAgo(selectedJob.completed_at) : null;

  // Build rows (actuals + comparison join)
  const compareByRef = comparisonData?.patients
    ? new Map(comparisonData.patients.map(p => [p.subject_reference, p]))
    : new Map();

  const allRows = (results?.patients || []).map(r => {
    const ref = `Patient/${r.patient_id}`;
    const cmp = compareByRef.get(ref);
    const errorPhase = r.error_phase;
    const phaseLabel = errorPhase === 'gather'         ? "Couldn't fetch patient data"
      : errorPhase === 'gather_partial' ? 'Some data missing'
      : errorPhase === 'evaluate'       ? 'Calculation failed'
      : errorPhase === 'submit'         ? "Couldn't submit patient data to the measure server"
      : (r.populations?.error === true || r.status === 'error') ? 'Unknown error'
      : null;
    return {
      resultId:    r.id,
      patientId:   r.patient_id,
      name:        r.patient_name || r.name || '',
      error:       phaseLabel,
      errorMessage: r.error_message || null,
      act:         toCounts(r.populations),
      exp:         cmp?.expected ? toCounts(cmp.expected) : undefined,
      match:       cmp ? cmp.match : undefined,
      mismatches:  cmp?.mismatches || [],
    };
  });

  const showStatusCol = expectedLoaded || allRows.some(r => r.error !== null);

  // Sort: failures first when expectedLoaded
  const sortedRows = expectedLoaded
    ? [...allRows].sort((a, b) => Number(!isFailure(a)) - Number(!isFailure(b)))
    : allRows;

  // Chip counts
  const failures    = allRows.filter(isFailure).length;
  const errorCount  = allRows.filter(r => r.error !== null).length;
  const missCount   = allRows.filter(r => r.error === null && r.match === false).length;

  useEffect(() => {
    if (loading) return;
    if (filterInitRef.current === selectedJobId) return;
    filterInitRef.current = selectedJobId;
    setFilter(expectedLoaded && failures > 0 ? 'failures' : 'all');
  }, [loading, selectedJobId, expectedLoaded, failures]);

  const chipCounts = {
    all:    allRows.length,
    failures,
    num:    allRows.filter(r => r.act.num).length,
    denom:  allRows.filter(r => r.act.den && !r.act.num).length,
    excl:   allRows.filter(r => r.act.denExc).length,
    notden: allRows.filter(r => !r.act.den).length,
  };

  // Normalize filter: if chip isn't in current list, fall back to 'all'
  const chips = expectedLoaded
    ? ['failures', 'all', 'num', 'denom', 'excl']
    : ['all', 'num', 'denom', 'excl', 'notden'];
  const activeFilter = chips.includes(filter) ? filter : 'all';

  // Apply filter
  const filteredRows = sortedRows.filter(row => {
    switch (activeFilter) {
      case 'failures': return isFailure(row);
      case 'num':      return row.act.num;
      case 'denom':    return row.act.den && !row.act.num;
      case 'excl':     return row.act.denExc;
      case 'notden':   return !row.act.den;
      default:         return true;
    }
  });

  // Search
  const q = query.trim().toLowerCase();
  const visibleRows = filteredRows.filter(row => {
    if (!q) return true;
    return row.name.toLowerCase().includes(q) || (row.patientId || '').toLowerCase().includes(q);
  });

  // Patients card header pill — only show comparison language when comparison is enabled
  const headerPill = !adminSettings.comparison_enabled
    ? null
    : expectedLoaded
      ? failures > 0
        ? <StatusPill kind="miss">{failures} need review</StatusPill>
        : <StatusPill kind="match">All match expected</StatusPill>
      : <StatusPill kind="neutral">actual only · no expected loaded</StatusPill>;

  // Column count for colSpan in expanded rows
  const colCount = showStatusCol ? 5 : 4;

  const CHIP_LABEL = {
    failures: 'Failures', all: 'All patients', num: 'In numerator',
    denom: 'Denom only', excl: 'Excluded', notden: 'Not in denom',
  };

  const CHIP_DEFS = expectedLoaded
    ? [
        { id: 'failures', label: 'Failures', count: chipCounts.failures, danger: true,
          detail: errorCount > 0 && missCount > 0 ? `${errorCount} err · ${missCount} miss` : null },
        { id: 'all',      label: 'All patients', count: chipCounts.all },
        { id: 'num',      label: 'In numerator', count: chipCounts.num },
        { id: 'denom',    label: 'Denom only',   count: chipCounts.denom },
        { id: 'excl',     label: 'Excluded',     count: chipCounts.excl },
      ]
    : [
        { id: 'all',    label: 'All patients',  count: chipCounts.all },
        { id: 'num',    label: 'In numerator',  count: chipCounts.num },
        { id: 'denom',  label: 'Denom only',    count: chipCounts.denom },
        { id: 'excl',   label: 'Excluded',      count: chipCounts.excl },
        { id: 'notden', label: 'Not in denom',  count: chipCounts.notden },
      ];

  // ─── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className={styles.page}>
      {/* Page header */}
      <div className={styles.pageHeader}>
        <div>
          {measureName && <div className={styles.eyebrow}>{measureDisplayLabel(selectedJob?.measure_id, measureName)}</div>}
          <h1 className={styles.title}>Results</h1>
          {period && <div className={styles.sub}><span className={styles.mono}>{period}</span></div>}
          {completedAgo && (
            <div className={styles.sub}>
              <span className={styles.completeBadge}>Complete</span>{' '}Calculated {completedAgo}
            </div>
          )}
          {selectedJob && (
            <div className={styles.sub}>
              <span>Queued {formatDateTime(selectedJob.created_at)}</span>
              {' · '}
              <span>Started {selectedJob.started_at ? formatDateTime(selectedJob.started_at) : '—'}</span>
              {' · '}
              <span>Duration {formatDuration(selectedJob.started_at || (selectedJob.completed_at ? selectedJob.created_at : null), selectedJob.completed_at)}</span>
            </div>
          )}
        </div>
        <div className={styles.headerActions}>
          {jobs.length > 0 && (
            <div className={styles.jobSelectGroup}>
              <label htmlFor="job-run-select" className={styles.jobSelectLabel}>Job run</label>
              <select id="job-run-select" className={styles.jobSelect} value={selectedJobId} onChange={handleJobChange}>
                {jobs.map(job => (
                  <option key={job.id} value={job.id}>
                    {measureOptionLabel(job.measure_id, job.measure_name)}
                    {job.period_start ? ` (${job.period_start})` : ''}
                  </option>
                ))}
              </select>
            </div>
          )}
          {selectedJobId && results && (
            <>
              <button
                className={styles.btnGhost}
                onClick={() => exportCsv(results.patients || [], selectedJob?.measure_id, measureName, period)}
                disabled={(results.patients || []).length === 0}
              >
                Export CSV
              </button>
              <button
                className={styles.btnGhost}
                onClick={handleDownloadBundle}
                disabled={(results.patients || []).length === 0 || bundleDownloading}
                aria-busy={bundleDownloading}
              >
                {bundleDownloading ? 'Downloading…' : 'Download FHIR JSON'}
              </button>
              {selectedJob && (
                <button
                  className={styles.btnAccent}
                  onClick={handleRerun}
                  disabled={rerunning}
                  aria-busy={rerunning}
                >
                  {rerunning ? 'Starting…' : 'Re-run'}
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {/* Loading skeleton */}
      {loading && (
        <div role="status" aria-label="Loading results">
          <div className={styles.cardsRow}>
            {[1, 2].map(i => <div key={i} className={`skeleton ${styles.skeletonCard}`} />)}
          </div>
        </div>
      )}

      {/* Error */}
      {!loading && error && (
        <div className={styles.errorState} role="alert">
          <p>Error loading results</p>
          <p className={styles.errorDetail}>{error}</p>
          <button className={styles.retryBtn} onClick={loadResults}>Retry</button>
        </div>
      )}

      {/* Empty */}
      {!loading && !error && !selectedJobId && (
        <div className={styles.emptyState}>
          <p>No results yet.</p>
          <p className={styles.emptyHint}>Run a calculation from the Jobs page.</p>
        </div>
      )}

      {/* Content */}
      {!loading && !error && selectedJobId && results && (
        <>
          {/* Stat cards */}
          <div className={styles.cardsRow}>
            <div className={styles.card}>
              <div className={styles.cardTopRow}>
                <div>
                  <div className={styles.cardEyebrow}>Performance rate</div>
                  <div className={styles.cardSub}>Numerator ÷ (Denominator − Exclusions)</div>
                </div>
              </div>
              <div className={styles.rateRow}>
                {perfRate !== undefined && perfRate !== null ? (
                  <div className={styles.rateNum}>
                    {typeof perfRate === 'number' ? perfRate.toFixed(1) : perfRate}
                    <span className={styles.ratePct}>%</span>
                  </div>
                ) : (
                  <div className={styles.rateNum}>—</div>
                )}
              </div>
            </div>

            <div className={styles.card}>
              <div className={styles.cardTopRow}>
                <div className={styles.cardEyebrow}>Populations</div>
                {ip != null && <div className={styles.cardSub}>of {ip.toLocaleString()} IP</div>}
              </div>
              {ip != null && (
                <>
                  <div className={styles.distBarWrap}>
                    <DistBar ip={ip} den={den || 0} num={num || 0} exc={exc || 0} height={6} />
                  </div>
                  <div className={styles.popGrid}>
                    {[
                      ['Initial Pop.', ip, 'text'],
                      ['Denominator', den, 'text'],
                      ['Numerator', num, 'accent'],
                      ['Exclusions', exc, 'warn'],
                    ].map(([label, val, tone]) => (
                      <div key={label} className={styles.popRow}>
                        <span className={styles.popLabel}>{label}</span>
                        <span className={styles.popVal} data-tone={tone}>
                          {val != null ? val.toLocaleString() : '--'}
                        </span>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          </div>

          {/* Patients card */}
          <div className={`${styles.card} ${styles.cardFlush}`}>
            {/* Table header */}
            <div className={styles.tableHeader}>
              <span className={styles.tableTitle}>Patients</span>
              <span className={styles.tableBadge}>{allRows.length.toLocaleString()}</span>
              <span className={styles.headerPill}>{headerPill}</span>
              <div className={styles.chipsRow}>
                {CHIP_DEFS.map(c => (
                  <FilterChip
                    key={c.id}
                    chipId={c.id}
                    label={c.label}
                    count={c.count}
                    active={activeFilter === c.id}
                    danger={c.danger}
                    onClick={setFilter}
                  />
                ))}
              </div>
            </div>

            {/* Legend strip */}
            {expectedLoaded && (
              <details className={styles.legendDetails}>
                <summary className={styles.legendSummary}>How to read results ▸</summary>
                <div className={styles.legendStrip}>
                  <span className={styles.legendLabel}>Per-patient verdict comparing actual vs. expected fixture:</span>
                  <span className={styles.legendItem}>
                    <StatusPill kind="match">✓ match</StatusPill>
                    <span className={styles.legendDesc}>all 5 populations agree</span>
                  </span>
                  <span className={styles.legendItem}>
                    <StatusPill kind="miss">! N mismatched</StatusPill>
                    <span className={styles.legendDesc}>N populations differ — click row for breakdown</span>
                  </span>
                  <span className={styles.legendHint}>IP · Denom · DnEx · Numer · NmEx</span>
                </div>
              </details>
            )}

            {/* Table */}
            <div className={styles.tableScroll}>
            <table aria-label="Patient results">
              <thead>
                <tr>
                  <th style={{ width: 32 }}></th>
                  <th style={{ width: 160 }}>Patient ID</th>
                  <th>Name</th>
                  {showStatusCol && <th style={{ width: 140 }}>Status</th>}
                  <th style={{ width: 340 }}>Population summary</th>
                </tr>
              </thead>
              <tbody>
                {visibleRows.length === 0 ? (
                  <tr>
                    <td colSpan={colCount} className={styles.emptyRow}>
                      <div>
                        {q
                          ? `No patients match "${q}".`
                          : `No patients in '${CHIP_LABEL[activeFilter] || activeFilter}'.`}
                      </div>
                      <button
                        className={styles.clearFiltersBtn}
                        onClick={() => { setFilter('all'); setQuery(''); }}
                        type="button"
                      >
                        Clear filters
                      </button>
                    </td>
                  </tr>
                ) : (
                  visibleRows.map(row => {
                    const isExpanded = expandedId === row.resultId;
                    const isMismatch = !row.error && expectedLoaded && row.match === false;
                    const isError = row.error !== null;

                    // Status pill content
                    let pill = null;
                    if (isError) {
                      const excerpt = row.errorMessage
                        ? row.errorMessage.slice(0, 60) + (row.errorMessage.length > 60 ? '…' : '')
                        : null;
                      pill = (
                        <span className={styles.errorPillGroup}>
                          <StatusPill kind="error">⚠ {row.error}</StatusPill>
                          {excerpt && <span className={styles.errorExcerpt}>{excerpt}</span>}
                        </span>
                      );
                    } else if (expectedLoaded) {
                      if (row.match === true) {
                        pill = (
                          <StatusPill kind="match" title="All 5 populations (IP, Denom, Denom Exclusion, Numer, Numer Exclusion) match the expected fixture for this patient.">
                            ✓ match
                          </StatusPill>
                        );
                      } else if (row.match === false) {
                        const mismatchCount = row.mismatches?.length > 0
                          ? row.mismatches.length
                          : POP_BREAKDOWN.filter(({ key }) => (row.act[key] ?? 0) !== (row.exp?.[key] ?? 0)).length;
                        const diffCodes = row.mismatches?.map(m => {
                          if (m === 'initial-population')     return 'IP';
                          if (m === 'denominator')            return 'Denom';
                          if (m === 'denominator-exclusion')  return 'DnEx';
                          if (m === 'numerator')              return 'Numer';
                          if (m === 'numerator-exclusion')    return 'NmEx';
                          return m;
                        }).join(', ') || null;
                        pill = (
                          <StatusPill
                            kind="miss"
                            title={diffCodes ? `Disagrees on: ${diffCodes}. Click row to see breakdown.` : 'Click row to see breakdown.'}
                          >
                            ! {mismatchCount} mismatched
                          </StatusPill>
                        );
                      }
                    }

                    const rowCls = [
                      styles.patientRow,
                      isMismatch ? styles.rowMiss : '',
                      isError    ? styles.rowError : '',
                    ].filter(Boolean).join(' ');

                    return (
                      <React.Fragment key={row.resultId}>
                        <tr
                          className={rowCls}
                          onClick={() => setExpandedId(isExpanded ? null : row.resultId)}
                          tabIndex={0}
                          role="button"
                          aria-expanded={isExpanded}
                          onKeyDown={(e) => {
                            if (e.key === ' ' || e.key === 'Enter') {
                              e.preventDefault();
                              setExpandedId(isExpanded ? null : row.resultId);
                            }
                          }}
                        >
                          <td>
                            <span className={styles.expandToggle}>
                              {isExpanded
                                ? <svg width="11" height="11" viewBox="0 0 11 11" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><path d="M2 4l3.5 3.5L9 4" /></svg>
                                : <svg width="11" height="11" viewBox="0 0 11 11" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><path d="M4 2l3.5 3.5L4 9" /></svg>
                              }
                            </span>
                          </td>
                          <td className={styles.idCell}><TruncatedId id={row.patientId} /></td>
                          <td className={styles.patientName}>{row.name || '—'}</td>
                          {showStatusCol && <td>{pill}</td>}
                          <td>
                            <PopulationDots
                              act={row.act}
                              exp={expectedLoaded && !row.error ? row.exp : undefined}
                            />
                          </td>
                        </tr>
                        {isExpanded && (
                          <ExpandedPanel
                            row={row}
                            expectedLoaded={expectedLoaded}
                            colSpan={colCount}
                            popDescriptions={results?.population_descriptions}
                            definedPops={results?.defined_populations}
                          />
                        )}
                      </React.Fragment>
                    );
                  })
                )}
              </tbody>
            </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

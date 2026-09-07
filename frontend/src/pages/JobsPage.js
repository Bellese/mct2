import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Link, useNavigate, useLocation } from 'react-router-dom';
import styles from './JobsPage.module.css';
import { getJobs, getMeasures, getGroups, createJob, cancelJob, deleteJob } from '../api/client';
import { useToast } from '../components/Toast';
import KebabMenu from '../components/KebabMenu';
import ConfirmDialog from '../components/ConfirmDialog';
import PulseDot from '../components/PulseDot';
import { TrashIcon, ViewIcon, SparkIcon, PlusIcon, XIcon } from '../components/Icons';
import { useSearch } from '../contexts/SearchContext';
import { useConnection } from '../contexts/ConnectionContext';
import PeriodPicker from '../components/PeriodPicker';
import { extractCmsId, measureDisplayLabel, measureOptionLabel, findMatchingGroup } from '../utils/measureFormat';
import { isActuallyRunning, isRunning, isComplete, selectActiveJob } from '../utils/jobStatus';
import { formatDateTime, formatDuration } from '../utils/dateFormat';

function formatElapsed(startStr) {
  if (!startStr) return '--';
  const diff = Math.floor((Date.now() - new Date(startStr)) / 1000);
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

function estimateRemaining(startStr, progress) {
  if (!startStr || !progress || progress >= 100) return null;
  const elapsed = Date.now() - new Date(startStr);
  const total = (elapsed / progress) * 100;
  const rem = Math.floor((total - elapsed) / 60000);
  return rem > 0 ? `~${rem}m remaining` : null;
}

function StatusBadge({ status }) {
  const s = (status || '').toLowerCase();
  if (s === 'completed' || s === 'complete') return <span className={`${styles.badge} ${styles.badgeOk}`}>Complete</span>;
  if (s === 'running' || s === 'in_progress' || s === 'in-progress') return <span className={`${styles.badge} ${styles.badgeRunning}`}><PulseDot />Running</span>;
  if (s === 'queued' || s === 'pending') return <span className={`${styles.badge} ${styles.badgeInfo}`}>Queued</span>;
  if (s === 'failed' || s === 'error') return <span className={`${styles.badge} ${styles.badgeErr}`}>Failed</span>;
  if (s === 'cancelled' || s === 'canceled') return <span className={`${styles.badge} ${styles.badgeErr}`}>Cancelled</span>;
  return <span className={styles.badge}>{status}</span>;
}


export default function JobsPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [jobs, setJobs] = useState([]);
  const [measures, setMeasures] = useState([]);
  // Distinguishes "haven't fetched yet" (initial mount) from "fetched and
  // the list is genuinely empty" — the reset effect below must not touch
  // formData.measure_id before the former, but must clear it for the
  // latter (#396).
  const [measuresLoaded, setMeasuresLoaded] = useState(false);
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showModal, setShowModal] = useState(false);
  const [formData, setFormData] = useState({ measure_id: '', group_id: '', period_start: '', period_end: '', workflow: 'direct_load' });
  const [creating, setCreating] = useState(false);
  const [confirmJob, setConfirmJob] = useState(null);
  const [deletingJobIds, setDeletingJobIds] = useState([]);
  const pollRef = useRef(null);
  const toast = useToast();
  const { query } = useSearch();
  const { mcs } = useConnection();

  const loadJobs = useCallback(async () => {
    try {
      const data = await getJobs();
      setJobs(Array.isArray(data) ? data : data.jobs || []);
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to load jobs');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMeasures = useCallback(async () => {
    try {
      const data = await getMeasures();
      const list = Array.isArray(data) ? data : data.measures || data.entry || [];
      setMeasures(list);
      setMeasuresLoaded(true);
    } catch {
      // Non-blocking for the Jobs page itself (jobs list / loading / error
      // state are untouched) — but the measure dropdown must not keep
      // offering the PREVIOUS MCS's measures after a failed refetch (e.g.
      // right after switching MCS and the new one being unreachable). Clear
      // it, same as MeasuresPage does in its own catch branch, and mark it
      // "loaded" so the reset effect below clears any now-stale
      // formData.measure_id instead of stranding it (#396).
      setMeasures([]);
      setMeasuresLoaded(true);
    }
  }, []);

  const loadGroups = useCallback(async () => {
    try {
      const data = await getGroups();
      setGroups(data.groups || []);
    } catch { /* non-blocking */ }
  }, []);

  useEffect(() => {
    loadJobs();
    loadMeasures();
    loadGroups();
    // mcs.id: re-fetch whenever the active MCS changes (#396), so activating
    // a different measure engine in Settings refreshes the measure/group list
    // this page's "New calculation" form is built from.
  }, [loadJobs, loadMeasures, loadGroups, mcs.id]);

  useEffect(() => {
    // Before the first fetch resolves there's nothing to reconcile against —
    // don't clear a selection (there shouldn't be one yet) just because the
    // initial state happens to be an empty array.
    if (!measuresLoaded) return;
    // Default when empty, and ALSO reset when the current selection is no
    // longer in the newly loaded list (e.g. after switching MCS) — otherwise
    // a stale measure_id survives and POST /jobs rejects it (#396). This
    // must also fire when the new list is itself empty (switching to an
    // MCS with zero measures) — an empty list is the most literal case of
    // "the current selection is absent from the newly loaded list", and
    // leaving a stale id in that state is exactly the bug this issue exists
    // to fix.
    const stillPresent = measures.some(m => m.id === formData.measure_id);
    if (!formData.measure_id || !stillPresent) {
      const nextId = measures[0]?.id || '';
      if (nextId !== formData.measure_id) {
        setFormData(prev => ({ ...prev, measure_id: nextId }));
      }
    }
  }, [measures, measuresLoaded]);

  useEffect(() => {
    const hasActive = jobs.some(j => isRunning(j.status) || j.delete_requested);
    if (hasActive) {
      pollRef.current = setInterval(loadJobs, 3000);
    } else {
      if (pollRef.current) clearInterval(pollRef.current);
    }
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [jobs, loadJobs]);

  useEffect(() => {
    if (!showModal) return;
    const y = new Date().getFullYear();
    setFormData(p => ({ ...p, period_start: `${y}-01-01`, period_end: `${y}-12-31` }));
  }, [showModal]);

  useEffect(() => {
    if (!formData.measure_id || !groups.length) return;
    const match = findMatchingGroup(formData.measure_id, groups);
    setFormData(p => ({ ...p, group_id: match != null ? String(match.id) : '' }));
  }, [formData.measure_id, groups]);

  const handleCreateJob = async (e) => {
    e.preventDefault();
    if (!formData.measure_id) { toast.error('Please select a measure'); return; }
    setCreating(true);
    try {
      const created = await createJob({
        measure_id: formData.measure_id,
        group_id: formData.group_id || undefined,
        period_start: formData.period_start || undefined,
        period_end: formData.period_end || undefined,
        workflow: formData.workflow,
      });
      toast.success('Calculation started');
      if (created?.submit_data_mode === 'base-fallback') {
        toast.warning('MCS does not support DEQM STU5 $deqm-submit-data — falling back to base $submit-data.');
      }
      setShowModal(false);
      setFormData(prev => ({ ...prev, period_start: '', period_end: '' }));
      loadJobs();
    } catch (err) {
      toast.error(`Failed to create job: ${err.message}`);
    } finally {
      setCreating(false);
    }
  };

  const handleCancel = async (jobId) => {
    try {
      await cancelJob(jobId);
      toast.success('Job cancelled');
      loadJobs();
    } catch (err) {
      toast.error(`Failed to cancel: ${err.message}`);
    }
  };

  const handleDeleteConfirmed = async () => {
    const job = confirmJob;
    setConfirmJob(null);
    setDeletingJobIds(prev => [...prev, job.id]);
    try {
      const result = await deleteJob(job.id);
      if (result?.delete_requested) {
        toast.warning('Deletion requested — job will disappear once background work stops.');
      } else {
        toast.success('Job deleted');
      }
      await loadJobs();
    } catch (err) {
      toast.error(`Failed to delete: ${err.message}`);
    } finally {
      setDeletingJobIds(prev => prev.filter(id => id !== job.id));
    }
  };

  const getMeasureName = (job) => {
    const name = job.measure_name || (() => {
      const m = measures.find(m => m.id === job.measure_id);
      return m ? (m.resource?.title || m.resource?.name || m.title || m.name || null) : null;
    })();
    return measureDisplayLabel(job.measure_id, name);
  };

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const newCalcId = params.get('newCalc');
    if (!newCalcId || !measures.length) return;
    const exists = measures.some(m => m.id === newCalcId);
    setFormData(prev => ({ ...prev, measure_id: exists ? newCalcId : (prev.measure_id || measures[0]?.id || '') }));
    setShowModal(true);
    navigate(location.pathname, { replace: true });
  }, [location.search, measures, navigate, location.pathname]);

  const getProgress = (job) => {
    if (job.progress !== undefined && job.progress !== null) return job.progress;
    const proc = job.processed_patients ?? job.patients_processed ?? 0;
    if (proc && job.total_patients) return Math.round((proc / job.total_patients) * 100);
    return 0;
  };

  const getCohortName = (job) => {
    if (job.group_name) return job.group_name;
    const group = groups.find(g => String(g.id) === String(job.group_id));
    return group?.name || job.cohort || job.group_id || 'All patients';
  };

  const getPatientCount = (job) => {
    const processed = job.processed_patients ?? job.patients_processed;
    const total = job.total_patients;
    if (isRunning(job.status) && total > 0) {
      return `${(processed ?? 0).toLocaleString()} / ${total.toLocaleString()}`;
    }
    if (isRunning(job.status)) return '--';
    if (total > 0) return total.toLocaleString();
    if (processed > 0) return processed.toLocaleString();
    return '--';
  };

  const q = query.trim().toLowerCase();
  const activeJob = selectActiveJob(jobs);
  const filteredJobs = jobs.filter(j => {
    if (!q) return true;
    return getMeasureName(j).toLowerCase().includes(q) || (j.id || '').toLowerCase().includes(q);
  });

  const runningCount = jobs.filter(j => isActuallyRunning(j.status)).length;
  const queuedCount = jobs.filter(j => { const s = (j.status || '').toLowerCase(); return s === 'queued' || s === 'pending'; }).length;

  return (
    <div className={styles.page}>
      <div className={styles.pageHeader}>
        <div>
          <div className={styles.eyebrow}>Calculations</div>
          <h1 className={styles.title}>Jobs</h1>
          <div className={styles.sub}>
            {runningCount} running ·{' '}
            {queuedCount > 0 && <>{queuedCount} queued ·{' '}</>}
            {jobs.filter(j => isComplete(j.status)).length} complete ·{' '}
            {jobs.filter(j => (j.status || '').toLowerCase() === 'failed').length} failed
          </div>
        </div>
        <button className={styles.btnPrimary} onClick={() => setShowModal(true)}>
          <PlusIcon /> New calculation
        </button>
      </div>

      {/* Active job hero card */}
      {activeJob && (() => {
        const progress = getProgress(activeJob);
        const proc = activeJob.processed_patients ?? activeJob.patients_processed ?? 0;
        const total = activeJob.total_patients;
        const batches = activeJob.batches_completed ?? 0;
        const totalBatches = activeJob.total_batches ?? 0;
        return (
          <div className={styles.heroCard}>
            <div className={styles.heroTop}>
              <div>
                <div className={styles.heroMeta}>
                  <StatusBadge status={activeJob.status} />
                  <span className={styles.heroId}>{activeJob.id}</span>
                </div>
                <div className={styles.heroName}>{getMeasureName(activeJob)}</div>
                <div className={styles.heroSub}>
                  {activeJob.period_start && activeJob.period_end && (
                    <span className={styles.mono}>{activeJob.period_start} → {activeJob.period_end}</span>
                  )}
                  <span>Cohort: {getCohortName(activeJob)}</span>
                  {isActuallyRunning(activeJob.status) && activeJob.started_at && (
                    <span>Elapsed {formatElapsed(activeJob.started_at)}</span>
                  )}
                </div>
              </div>
              <button className={styles.btnGhost} onClick={() => handleCancel(activeJob.id)}>
                <XIcon /> Cancel
              </button>
            </div>
            <div className={styles.heroProgress}>
              <div className={styles.heroProgressTop}>
                <span className={styles.heroPct}>{progress}<span className={styles.heroPctUnit}>%</span></span>
                {total > 0
                  ? <span className={styles.heroProgressLabel}>{proc.toLocaleString()} of {total.toLocaleString()} patients</span>
                  : <span className={styles.heroProgressLabel}>Preparing…</span>
                }
                {estimateRemaining(activeJob.started_at, progress) && (
                  <span className={styles.heroEta}>{estimateRemaining(activeJob.started_at, progress)}</span>
                )}
              </div>
              <div className={styles.progressTrack}>
                <div className={styles.progressFill} style={{ width: `${progress}%` }} />
              </div>
            </div>
            {totalBatches > 0 && (
              <div className={styles.batchSection}>
                <div className={styles.batchHeader}>
                  <span className={styles.batchLabel}>Batches</span>
                  <span className={styles.batchMeta}>{batches} / {totalBatches}</span>
                </div>
                <div className={styles.batchGrid} style={{ gridTemplateColumns: `repeat(${Math.min(totalBatches, 40)}, 1fr)` }}>
                  {Array.from({ length: Math.min(totalBatches, 40) }, (_, i) => {
                    const s = i < batches ? 'done' : i === batches ? 'active' : 'pending';
                    return <div key={i} className={`${styles.batchCell} ${styles[`batchCell_${s}`]}`} />;
                  })}
                </div>
              </div>
            )}
          </div>
        );
      })()}

      {/* Jobs table */}
      {loading && (
        <div className={styles.card} role="status" aria-label="Loading jobs">
          <div className={styles.tableScroll}>
            <table><thead><tr><th className={styles.measureCell}>Measure</th><th>Period</th><th>Cohort</th><th>Patients</th><th>Status</th><th>Queued</th><th>Started</th><th>Duration</th><th style={{ width: 50 }}></th></tr></thead>
              <tbody>{[1,2,3].map(i => (<tr key={i}>{[180,100,80,80,40,80,80,60,50].map((w,j) => (<td key={j}><div className="skeleton" style={{ height: 14, width: w }} /></td>))}</tr>))}</tbody>
            </table>
          </div>
        </div>
      )}

      {!loading && error && (
        <div className={styles.errorState} role="alert">
          <p>{error}</p>
          <button className={styles.retryBtn} onClick={loadJobs}>Retry</button>
        </div>
      )}

      {!loading && !error && (
        <div className={styles.card}>
          <div className={styles.cardHeader}>
            <span className={styles.cardTitle}>All jobs</span>
            <span className={styles.cardCount}>{filteredJobs.length}</span>
          </div>
          <div className={styles.tableScroll}>
          <table aria-label="Calculation jobs">
            <thead>
              <tr>
                <th className={styles.measureCell}>Measure</th>
                <th>Period</th>
                <th>Cohort</th>
                <th>Patients</th>
                <th>Status</th>
                <th>Queued</th>
                <th>Started</th>
                <th>Duration</th>
                <th style={{ width: 40 }}></th>
              </tr>
            </thead>
            <tbody>
              {filteredJobs.length === 0 ? (
                <tr><td colSpan={9} className={styles.emptyRow}>
                  {q ? `No jobs match "${q}".` : 'No calculations yet. Click "New calculation" to get started.'}
                </td></tr>
              ) : (
                filteredJobs.map((job) => {
                  const running = isRunning(job.status);
                  const complete = isComplete(job.status);
                  const deleting = job.delete_requested || deletingJobIds.includes(job.id);
                  const isFallback = job.submit_data_mode === 'base-fallback';
                  return (
                    <tr
                      key={job.id}
                      className={`${styles.row} ${complete ? styles.rowClickable : ''}`}
                      onClick={() => complete && navigate(`/results/${job.id}`)}
                      style={{ cursor: complete ? 'pointer' : 'default' }}
                    >
                      <td data-label="Measure" className={styles.measureCell}>
                        <div className={styles.jobMeta}>
                          <div className={styles.jobName}>{getMeasureName(job)}</div>
                          <div className={`${styles.mono} ${styles.jobId}`}>{job.id}</div>
                        </div>
                      </td>
                      <td data-label="Period" className={`${styles.mono} ${styles.periodCell}`}>
                        {job.period_start && job.period_end
                          ? <><span style={{whiteSpace:'nowrap'}}>{job.period_start}</span>{' – '}<span style={{whiteSpace:'nowrap'}}>{job.period_end}</span></>
                          : '--'}
                      </td>
                      <td data-label="Cohort" className={styles.cohortCell}>{getCohortName(job)}</td>
                      <td data-label="Patients" className={styles.patientCountCell}>{getPatientCount(job)}</td>
                      <td data-label="Status">
                        <StatusBadge status={job.status} />
                        {job.workflow === 'deqm_submit_data' && (
                          <span
                            className={`${styles.workflowTag} ${isFallback ? styles.workflowTagWarn : ''}`}
                            title={isFallback
                              ? 'MCS does not support DEQM STU5 $deqm-submit-data — base $submit-data fallback used.'
                              : 'DEQM STU5 $deqm-submit-data'}
                            aria-label={isFallback
                              ? 'DEQM — MCS does not support DEQM STU5 $deqm-submit-data — base $submit-data fallback used.'
                              : 'DEQM — DEQM STU5 $deqm-submit-data'}
                          >
                            DEQM{isFallback ? ' ⚠' : ''}
                          </span>
                        )}
                      </td>
                      <td data-label="Queued" className={styles.dateCell}>{formatDateTime(job.created_at)}</td>
                      <td data-label="Started" className={styles.dateCell}>{job.started_at ? formatDateTime(job.started_at) : '—'}</td>
                      <td data-label="Duration" className={styles.dateCell}>{formatDuration(job.started_at || (job.completed_at ? job.created_at : null), job.completed_at)}</td>
                      <td data-label="Actions">
                        <KebabMenu items={[
                          { label: 'View results', icon: <ViewIcon />, disabled: !complete, onClick: () => navigate(`/results/${job.id}`) },
                          { label: 'Re-run', icon: <SparkIcon />, onClick: () => {} },
                          { divider: true },
                          {
                            label: 'Delete',
                            icon: <TrashIcon />,
                            tone: 'destructive',
                            disabled: running || deleting,
                            hint: running ? 'cancel first' : undefined,
                            onClick: () => setConfirmJob(job),
                          },
                        ]} />
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={!!confirmJob}
        title="Delete this job?"
        body={<><strong>{confirmJob ? getMeasureName(confirmJob) : ''}</strong> and all patient-level results will be deleted. This cannot be undone.</>}
        confirmLabel="Delete job"
        tone="destructive"
        onCancel={() => setConfirmJob(null)}
        onConfirm={handleDeleteConfirmed}
      />

      {/* New Calculation modal */}
      {showModal && (
        <div className={styles.modalBackdrop} onClick={() => setShowModal(false)} role="dialog" aria-label="New calculation" aria-modal="true">
          <div className={styles.sheet} onClick={(e) => e.stopPropagation()}>
            <div className={styles.sheetHeader}>
              <span className={styles.sheetTitle}>New calculation</span>
              <button className={styles.sheetClose} onClick={() => setShowModal(false)} aria-label="Close"><XIcon /></button>
            </div>
            <form id="new-calc-form" onSubmit={handleCreateJob} className={styles.sheetBody}>
              <div className={styles.field}>
                <label className={styles.label} htmlFor="measure-select">Measure</label>
                <select id="measure-select" className={styles.select} value={formData.measure_id}
                  onChange={e => setFormData(p => ({ ...p, measure_id: e.target.value }))} required>
                  <option value="" disabled>Choose a measure…</option>
                  {measures.map((m, i) => (
                    <option key={m.id || i} value={m.id || ''}>{measureOptionLabel(m.id, m.resource?.title || m.resource?.name || m.title || m.name)}</option>
                  ))}
                </select>
              </div>
              <div className={styles.field}>
                <label className={styles.label} htmlFor="group-select">Patient group <span className={styles.labelHint}>(optional)</span></label>
                <select id="group-select" className={styles.select} value={formData.group_id}
                  onChange={e => setFormData(p => ({ ...p, group_id: e.target.value }))}>
                  <option value="">All patients (no group filter)</option>
                  {groups.map(g => {
                    const cmsId = extractCmsId(g.name || g.id);
                    const m = cmsId && measures.find(mx => mx.id === (g.name || g.id));
                    const label = m
                      ? measureOptionLabel(m.id, m.resource?.title || m.resource?.name || m.title || m.name)
                      : (cmsId || g.name || g.id);
                    return <option key={g.id} value={g.id}>{label} ({g.member_count} patients)</option>;
                  })}
                </select>
              </div>
              <div className={styles.field}>
                <label className={styles.label} htmlFor="workflow-select">Data submission workflow</label>
                <select id="workflow-select" className={styles.select} value={formData.workflow}
                  onChange={e => setFormData(p => ({ ...p, workflow: e.target.value }))}>
                  <option value="direct_load">Direct load — $everything (default)</option>
                  <option value="deqm_submit_data">DEQM Data Exchange — $submit-data (STU5)</option>
                </select>
              </div>
              <PeriodPicker
                periodStart={formData.period_start}
                periodEnd={formData.period_end}
                onChange={(start, end) => setFormData(p => ({ ...p, period_start: start, period_end: end }))}
              />
            </form>
            <div className={styles.sheetFooter}>
              <button type="button" className={styles.btnGhost} onClick={() => setShowModal(false)}>Cancel</button>
              <button type="submit" form="new-calc-form" className={styles.btnPrimary} onClick={handleCreateJob} disabled={creating} aria-busy={creating}>
                {creating ? 'Starting…' : 'Start calculation'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import JobsPage from './JobsPage';
import ConnectionContext from '../contexts/ConnectionContext';
import { ToastProvider } from '../components/Toast';
import * as api from '../api/client';

// Covers Task 6: per-job data submission workflow selector + visibility into
// the DEQM STU5 -> base $submit-data fallback when the active MCS doesn't
// implement $deqm-submit-data.
jest.mock('../api/client');

function Harness() {
  return (
    <ToastProvider>
      <ConnectionContext.Provider
        value={{
          cdr: { id: 'cdr-1', name: 'Local CDR', state: 'healthy' },
          mcs: { id: 'mcs-1', name: 'MCS', state: 'healthy', isReadOnly: false },
          refresh: jest.fn(),
        }}
      >
        <MemoryRouter>
          <JobsPage />
        </MemoryRouter>
      </ConnectionContext.Provider>
    </ToastProvider>
  );
}

const BASE_JOB = {
  id: 1,
  measure_id: 'CMS999',
  measure_name: 'Test Measure',
  period_start: '2025-01-01',
  period_end: '2025-12-31',
  cdr_url: 'http://cdr/fhir',
  group_id: null,
  status: 'complete',
  total_patients: 1,
  processed_patients: 1,
  failed_patients: 0,
  total_batches: 1,
  batches_completed: 1,
  delete_requested: false,
  created_at: '2026-08-21T00:00:00Z',
  started_at: '2026-08-21T00:00:01Z',
  completed_at: '2026-08-21T00:01:00Z',
  error_message: null,
};

describe('JobsPage — data submission workflow', () => {
  beforeEach(() => {
    api.getGroups = jest.fn().mockResolvedValue({ groups: [] });
    api.getMeasures = jest.fn().mockResolvedValue({ measures: [{ id: 'CMS999' }] });
    api.createJob = jest.fn().mockResolvedValue({ ...BASE_JOB, workflow: 'deqm_submit_data', submit_data_mode: 'base-fallback' });
  });

  test('modal defaults to direct load and sends the selected workflow', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    const workflowSelect = await screen.findByLabelText(/Data submission workflow/i);
    expect(workflowSelect.value).toBe('direct_load');
    await userEvent.selectOptions(workflowSelect, 'deqm_submit_data');
    const measureSelect = await screen.findByLabelText('Measure');
    await waitFor(() => expect(measureSelect.value).toBe('CMS999'));
    await userEvent.click(screen.getByRole('button', { name: /Start calculation/i }));
    await waitFor(() =>
      expect(api.createJob).toHaveBeenCalledWith(expect.objectContaining({ workflow: 'deqm_submit_data' }))
    );
  });

  test('creating a DEQM job that falls back to base mode shows a warning toast', async () => {
    // Coverage-audit gap fill: JobsPage.js fires toast.warning() when the
    // creation response comes back with submit_data_mode === 'base-fallback'.
    // No prior test asserted this toast actually renders.
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    const workflowSelect = await screen.findByLabelText(/Data submission workflow/i);
    await userEvent.selectOptions(workflowSelect, 'deqm_submit_data');
    const measureSelect = await screen.findByLabelText('Measure');
    await waitFor(() => expect(measureSelect.value).toBe('CMS999'));
    await userEvent.click(screen.getByRole('button', { name: /Start calculation/i }));
    expect(
      await screen.findByText(/MCS does not support DEQM STU5 \$deqm-submit-data — falling back to base \$submit-data\./i)
    ).toBeInTheDocument();
  });

  test('DEQM job with base-fallback shows the STU5 warning badge', async () => {
    api.getJobs = jest.fn().mockResolvedValue({
      jobs: [{ ...BASE_JOB, workflow: 'deqm_submit_data', submit_data_mode: 'base-fallback' }],
    });
    render(<Harness />);
    const badge = await screen.findByTitle(/does not support DEQM STU5/i);
    expect(badge).toHaveTextContent('DEQM');
    // The fallback explanation must survive without a mouse hover — a
    // screen reader needs an accessible name that carries the same
    // message as the title tooltip, not just the visible "DEQM ⚠" text.
    expect(screen.getByLabelText(/does not support DEQM STU5/i)).toBe(badge);
  });

  test('direct load jobs show no workflow badge', async () => {
    api.getJobs = jest.fn().mockResolvedValue({
      jobs: [{ ...BASE_JOB, workflow: 'direct_load', submit_data_mode: null }],
    });
    render(<Harness />);
    await screen.findByText(/Test Measure/);
    expect(screen.queryByText(/DEQM/)).not.toBeInTheDocument();
  });
});

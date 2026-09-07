import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import JobsPage from './JobsPage';
import ConnectionContext from '../contexts/ConnectionContext';
import { ToastProvider } from '../components/Toast';
import * as api from '../api/client';

// Covers #404: the "New calculation" patient-group dropdown is built from the
// CDR (GET /jobs/groups) but used to be keyed on mcs.id alone. Switching to a
// different CDR left the PREVIOUS CDR's Groups on offer, so submitting the
// form targeted a Group id that does not exist on the newly activated server.
jest.mock('../api/client');

function Harness({ cdrId, mcsId = 'mcs-a' }) {
  return (
    <ToastProvider>
      <ConnectionContext.Provider
        value={{
          cdr: { id: cdrId, name: 'CDR', state: 'healthy', isReadOnly: false },
          mcs: { id: mcsId, name: 'MCS', state: 'healthy', isReadOnly: false },
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

describe('JobsPage — patient group dropdown follows the active CDR (#404)', () => {
  test('refetches the group list when the active CDR changes', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    api.getMeasures = jest.fn().mockResolvedValue({ measures: [{ id: 'CMS999' }] });
    api.getGroups = jest
      .fn()
      .mockResolvedValueOnce({ groups: [{ id: 'group-from-cdr-a', name: 'Cohort A' }] })
      .mockResolvedValueOnce({ groups: [{ id: 'group-from-cdr-b', name: 'Cohort B' }] });

    const { rerender } = render(<Harness cdrId="cdr-a" />);

    await waitFor(() => expect(api.getGroups).toHaveBeenCalledTimes(1));
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    expect(await screen.findByRole('option', { name: /Cohort A/i })).toBeInTheDocument();

    // Activating a different CDR in Settings changes cdr.id. The group list is
    // CDR-derived, so it must refetch — without this the form keeps offering
    // Cohort A, which does not exist on the newly activated server.
    rerender(<Harness cdrId="cdr-b" />);

    await waitFor(() => expect(api.getGroups).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('option', { name: /Cohort B/i })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole('option', { name: /Cohort A/i })).not.toBeInTheDocument(),
    );
  });
});

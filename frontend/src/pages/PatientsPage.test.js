import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import PatientsPage from './PatientsPage';
import ConnectionContext from '../contexts/ConnectionContext';
import SearchContext from '../contexts/SearchContext';
import * as api from '../api/client';

jest.mock('../api/client');

// Issue #404. The Patients module answers one question for a participant who
// has just pointed Lenny at a CDR it has never seen: what patient cohorts
// exist here? So it lists every Group, unfiltered, and follows the active CDR.

function Harness({ cdrId = 'cdr-a', cdrName = 'Local CDR', state = 'healthy', query = '' }) {
  return (
    <SearchContext.Provider value={{ query, setQuery: jest.fn() }}>
      <ConnectionContext.Provider
        value={{
          cdr: { id: cdrId, name: cdrName, state, isReadOnly: false },
          mcs: { id: 'mcs-a', name: 'MCS', state: 'healthy', isReadOnly: false },
          refresh: jest.fn(),
        }}
      >
        <MemoryRouter>
          <PatientsPage />
        </MemoryRouter>
      </ConnectionContext.Provider>
    </SearchContext.Provider>
  );
}

describe('PatientsPage — listing cohorts on the active CDR (#404)', () => {
  test('renders one row per Group with name, id, type and member count', async () => {
    api.getPatientGroups = jest.fn().mockResolvedValue({
      groups: [
        { id: 'g1', name: 'CMS104-patients', type: 'person', member_count: 42, quantity: null },
        { id: 'g2', name: 'CMS71 cohort', type: 'person', member_count: 319, quantity: null },
      ],
    });

    render(<Harness />);

    expect(await screen.findByText('CMS104-patients')).toBeInTheDocument();
    expect(screen.getByText('CMS71 cohort')).toBeInTheDocument();
    expect(screen.getByText('g1')).toBeInTheDocument();
    expect(screen.getByText('42')).toBeInTheDocument();
    expect(screen.getByText('319')).toBeInTheDocument();
  });

  test('refetches the list when the active CDR changes', async () => {
    api.getPatientGroups = jest
      .fn()
      .mockResolvedValueOnce({ groups: [{ id: 'g1', name: 'Cohort A', type: 'person', member_count: 1 }] })
      .mockResolvedValueOnce({ groups: [{ id: 'g9', name: 'Cohort B', type: 'person', member_count: 2 }] });

    const { rerender } = render(<Harness cdrId="cdr-a" />);
    expect(await screen.findByText('Cohort A')).toBeInTheDocument();

    rerender(<Harness cdrId="cdr-b" />);

    expect(await screen.findByText('Cohort B')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('Cohort A')).not.toBeInTheDocument());
    expect(api.getPatientGroups).toHaveBeenCalledTimes(2);
  });

  test('a reachable CDR with zero Groups names it and says measures still run', async () => {
    api.getPatientGroups = jest.fn().mockResolvedValue({ groups: [] });

    render(<Harness cdrName="Attendee CDR" />);

    expect(await screen.findByText(/Attendee CDR/)).toBeInTheDocument();
    expect(screen.getByText(/All patients/i)).toBeInTheDocument();
  });

  test('an unreachable CDR shows the connectivity error, not the zero-Groups copy', async () => {
    api.getPatientGroups = jest.fn().mockRejectedValue(new Error('Cannot reach CDR to list groups.'));

    render(<Harness cdrName="Attendee CDR" />);

    expect(await screen.findByText(/Cannot reach/i)).toBeInTheDocument();
    expect(screen.queryByText(/All patients/i)).not.toBeInTheDocument();
  });

  test('the header search box filters rows by name and id', async () => {
    api.getPatientGroups = jest.fn().mockResolvedValue({
      groups: [
        { id: 'g1', name: 'CMS104-patients', type: 'person', member_count: 42 },
        { id: 'zz9', name: 'Severe OB comps', type: 'person', member_count: 7 },
      ],
    });

    const { rerender } = render(<Harness />);
    expect(await screen.findByText('CMS104-patients')).toBeInTheDocument();

    rerender(<Harness query="severe" />);
    expect(screen.getByText('Severe OB comps')).toBeInTheDocument();
    expect(screen.queryByText('CMS104-patients')).not.toBeInTheDocument();

    // ...and by id, not just name.
    rerender(<Harness query="g1" />);
    expect(screen.getByText('CMS104-patients')).toBeInTheDocument();
    expect(screen.queryByText('Severe OB comps')).not.toBeInTheDocument();
  });

  test('a Group sized only by quantity does not render as empty', async () => {
    api.getPatientGroups = jest.fn().mockResolvedValue({
      groups: [{ id: 'g1', name: 'Characteristic Cohort', type: 'person', member_count: 0, quantity: 319 }],
    });

    render(<Harness />);

    expect(await screen.findByText('319')).toBeInTheDocument();
    expect(screen.queryByText(/empty/i)).not.toBeInTheDocument();
  });

  test('a Group with neither members nor quantity renders an honest dash, not 0', async () => {
    api.getPatientGroups = jest.fn().mockResolvedValue({
      groups: [{ id: 'g1', name: 'Unknown Size Cohort', type: 'person', member_count: 0, quantity: null }],
    });

    render(<Harness />);

    expect(await screen.findByText('Unknown Size Cohort')).toBeInTheDocument();
    expect(screen.getByTestId('member-count-g1')).toHaveTextContent('—');
  });
});

import fs from 'fs';
import path from 'path';

describe('PatientsPage — architecture independence (#322)', () => {
  const FORBIDDEN_IMPORT_FRAGMENTS = [
    '/pages/JobsPage',
    '/pages/MeasuresPage',
    '/pages/ResultsPage',
    '/pages/ValidationPage',
    '/utils/jobStatus',
    '/utils/measureFormat',
  ];

  test('PatientsPage.js does not import measure-pipeline modules', () => {
    const source = fs.readFileSync(path.join(__dirname, 'PatientsPage.js'), 'utf8');
    const offenders = FORBIDDEN_IMPORT_FRAGMENTS.filter(f => source.includes(f));
    expect(offenders).toEqual([]);
  });
});

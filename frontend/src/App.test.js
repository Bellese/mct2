import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import App from './App';
import * as api from './api/client';

// Render smoke test for the ROUTES-derived chrome (#384): sidebar nav,
// header title, and search placeholder must all reflect ROUTES and the
// feature flags, since none of that is covered by App.routes.test.js's
// data-only parity checks. Pages themselves are stubbed — this test is
// about App.js's derivation, not page behavior.
jest.mock('./api/client');
jest.mock('./pages/MeasuresPage', () => () => <div>Measures page</div>);
jest.mock('./pages/JobsPage', () => () => <div>Jobs page</div>);
jest.mock('./pages/PatientsPage', () => () => <div>Patients page</div>);
jest.mock('./pages/ResultsPage', () => () => <div>Results page</div>);
jest.mock('./pages/ValidationPage', () => () => <div>Validation page</div>);
jest.mock('./pages/SettingsPage', () => () => <div>Settings page</div>);

function renderApp(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

function mockSettings({ validation = false, groups = false } = {}) {
  api.getHealth = jest.fn().mockResolvedValue({
    cdr: { status: 'healthy' },
    measure_engine: { status: 'healthy' },
  });
  api.getAdminSettings = jest.fn().mockResolvedValue({
    validation_enabled: validation,
    groups_enabled: groups,
  });
}

describe('App — nav / title / search derivation from ROUTES', () => {
  test('validation flag off: Validation is hidden but Patients is not, /jobs resolves its title and placeholder', async () => {
    mockSettings({ validation: false, groups: false });

    renderApp('/jobs');
    await waitFor(() => expect(api.getAdminSettings).toHaveBeenCalled());

    const nav = screen.getByRole('navigation', { name: 'Main navigation' });
    expect(within(nav).getByRole('link', { name: /Measures/ })).toBeInTheDocument();
    expect(within(nav).getByRole('link', { name: /Jobs/ })).toBeInTheDocument();
    expect(within(nav).getByRole('link', { name: /Results/ })).toBeInTheDocument();
    // Patients is always on — a participant surveying an unfamiliar CDR must
    // not have to find an admin flag first (#404). groups_enabled now gates
    // only the parked $evaluate backend.
    expect(within(nav).getByRole('link', { name: /Patients/ })).toBeInTheDocument();
    expect(within(nav).queryByRole('link', { name: /Validation/ })).not.toBeInTheDocument();

    const header = screen.getByRole('banner');
    expect(within(header).getByText('Jobs')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Search jobs…')).toBeInTheDocument();
  });

  test('/patients resolves its title and placeholder', async () => {
    mockSettings({ validation: true, groups: true });

    renderApp('/patients');
    const nav = screen.getByRole('navigation', { name: 'Main navigation' });
    await waitFor(() => expect(within(nav).getByRole('link', { name: /Patients/ })).toBeInTheDocument());
    expect(within(nav).getByRole('link', { name: /Validation/ })).toBeInTheDocument();

    const header = screen.getByRole('banner');
    expect(within(header).getByText('Patients')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Search patient groups…')).toBeInTheDocument();
  });

  test('/groups redirects to /patients so old bookmarks keep working', async () => {
    mockSettings();
    renderApp('/groups');
    expect(await screen.findByText('Patients page')).toBeInTheDocument();
  });

  test('/ redirects to /measures', async () => {
    mockSettings();
    renderApp('/');
    expect(await screen.findByText('Measures page')).toBeInTheDocument();
  });
});

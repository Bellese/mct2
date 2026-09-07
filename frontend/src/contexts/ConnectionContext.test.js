import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConnectionProvider, useConnection } from './ConnectionContext';
import * as api from '../api/client';

jest.mock('../api/client');

function Probe() {
  const { cdr, mcs, refresh } = useConnection();
  return (
    <div>
      <span data-testid="mcs-id">{mcs.id}</span>
      <span data-testid="mcs-name">{mcs.name}</span>
      <span data-testid="mcs-state">{mcs.state}</span>
      <span data-testid="mcs-readonly">{String(mcs.isReadOnly)}</span>
      <span data-testid="cdr-id">{String(cdr.id)}</span>
      <span data-testid="cdr-name">{cdr.name}</span>
      <button onClick={refresh}>refresh</button>
    </div>
  );
}

describe('ConnectionContext — provider exposes mcs/cdr identity from health (#396)', () => {
  test('exposes mcs id/name/isReadOnly and cdr id/name from GET /health', async () => {
    api.getHealth = jest.fn().mockResolvedValue({
      cdr: { status: 'healthy', name: 'Local CDR', id: 'cdr-1' },
      measure_engine: {
        status: 'healthy',
        name: 'Alphora Sandbox',
        id: 'mcs-2',
        is_read_only: true,
      },
    });

    render(
      <ConnectionProvider>
        <Probe />
      </ConnectionProvider>,
    );

    await waitFor(() => expect(screen.getByTestId('mcs-id')).toHaveTextContent('mcs-2'));
    expect(screen.getByTestId('mcs-name')).toHaveTextContent('Alphora Sandbox');
    expect(screen.getByTestId('mcs-state')).toHaveTextContent('healthy');
    expect(screen.getByTestId('mcs-readonly')).toHaveTextContent('true');
    expect(screen.getByTestId('cdr-name')).toHaveTextContent('Local CDR');
  });

  test('an unreachable measure_engine section reports isReadOnly false and no id, not a stale one', async () => {
    api.getHealth = jest.fn().mockResolvedValue({
      cdr: { status: 'healthy', name: 'Local CDR', id: 'cdr-1' },
      measure_engine: { status: 'error', name: '', error_details: { hint: 'Connection refused' } },
    });

    render(
      <ConnectionProvider>
        <Probe />
      </ConnectionProvider>,
    );

    await waitFor(() => expect(screen.getByTestId('mcs-name')).toHaveTextContent(''));
    expect(screen.getByTestId('mcs-readonly')).toHaveTextContent('false');
  });

  test('fails closed: preserves isReadOnly through a total health-check failure (#396)', async () => {
    api.getHealth = jest.fn()
      .mockResolvedValueOnce({
        cdr: { status: 'healthy', name: 'Local CDR', id: 'cdr-1' },
        measure_engine: { status: 'healthy', name: 'Alphora Sandbox', id: 'mcs-2', is_read_only: true },
      })
      .mockRejectedValueOnce(new Error('network down'));

    render(
      <ConnectionProvider>
        <Probe />
      </ConnectionProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('mcs-readonly')).toHaveTextContent('true'));

    await userEvent.click(screen.getByRole('button', { name: 'refresh' }));
    await waitFor(() => expect(api.getHealth).toHaveBeenCalledTimes(2));
    // A network failure must not silently re-enable Upload/Delete against a
    // server we previously confirmed was read-only.
    expect(screen.getByTestId('mcs-readonly')).toHaveTextContent('true');
  });

  test('fails closed: preserves isReadOnly when a later probe omits the measure_engine block (#396)', async () => {
    api.getHealth = jest.fn()
      .mockResolvedValueOnce({
        cdr: { status: 'healthy', name: 'Local CDR', id: 'cdr-1' },
        measure_engine: { status: 'healthy', name: 'Alphora Sandbox', id: 'mcs-2', is_read_only: true },
      })
      .mockResolvedValueOnce({ cdr: { status: 'healthy', name: 'Local CDR', id: 'cdr-1' } });

    render(
      <ConnectionProvider>
        <Probe />
      </ConnectionProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('mcs-readonly')).toHaveTextContent('true'));

    await userEvent.click(screen.getByRole('button', { name: 'refresh' }));
    await waitFor(() => expect(api.getHealth).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('mcs-readonly')).toHaveTextContent('true');
  });
});

describe('ConnectionContext — cdr identity from health (#404)', () => {
  test('surfaces cdr.id from GET /health so effects can key on the active CDR', async () => {
    api.getHealth = jest.fn().mockResolvedValue({
      cdr: { status: 'healthy', name: 'Attendee CDR', id: 'cdr-7', is_read_only: false },
      measure_engine: { status: 'healthy', name: 'Local MCS', id: 'mcs-2' },
    });

    render(
      <ConnectionProvider>
        <Probe />
      </ConnectionProvider>,
    );

    await waitFor(() => expect(screen.getByTestId('cdr-id')).toHaveTextContent('cdr-7'));
    expect(screen.getByTestId('cdr-name')).toHaveTextContent('Attendee CDR');
  });

  test('a network error clears cdr.id to null rather than leaving a stale one', async () => {
    api.getHealth = jest
      .fn()
      .mockResolvedValueOnce({
        cdr: { status: 'healthy', name: 'Attendee CDR', id: 'cdr-7' },
        measure_engine: { status: 'healthy', name: 'Local MCS', id: 'mcs-2' },
      })
      .mockRejectedValue(new Error('network down'));

    render(
      <ConnectionProvider>
        <Probe />
      </ConnectionProvider>,
    );

    await waitFor(() => expect(screen.getByTestId('cdr-id')).toHaveTextContent('cdr-7'));

    await userEvent.click(screen.getByText('refresh'));

    await waitFor(() => expect(screen.getByTestId('cdr-id')).toHaveTextContent('null'));
  });
});

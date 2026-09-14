import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TodayPage } from './TodayPage';

function renderToday() {
  return render(<TodayPage onStart={vi.fn()} onImport={vi.fn()} />);
}

function respondWithSummaries() {
  vi.mocked(fetch).mockImplementation(async (input) => {
    const spanish = String(input).endsWith('language=spanish');
    return Response.json({
      language: spanish ? 'spanish' : 'english',
      deck: spanish ? 'Languages::Spanish' : 'Languages::English',
      new: 0, learning: 0, review: 0, total: 0,
    });
  });
}

describe('Today plan status', () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('describes loading without claiming to probe Anki health', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}));
    renderToday();
    expect(screen.getByLabelText('Загрузка плана на сегодня')).toBeInTheDocument();
    expect(screen.queryByText(/Anki Core/)).not.toBeInTheDocument();
  });

  it('reports a loaded plan when both summaries succeed, including empty queues', async () => {
    respondWithSummaries();
    renderToday();
    expect(await screen.findByLabelText('План на сегодня загружен')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'На сегодня всё' })).toHaveLength(2);
    expect(screen.queryByText(/Anki Core/)).not.toBeInTheDocument();
  });

  it.each([
    ['Spanish', 'Испанский'],
    ['English', 'Английский'],
  ])('identifies the missing %s deck without reporting a Core outage', async (deck, name) => {
    vi.mocked(fetch).mockImplementation(async () => Response.json(
      { detail: `required deck does not exist: Languages::${deck}` }, { status: 409 },
    ));
    renderToday();
    expect(await screen.findByLabelText('Колода не найдена')).toBeInTheDocument();
    expect(screen.getByText(
      `В серверной коллекции не найдена колода «${name}» (Languages::${deck}).`,
    )).toBeInTheDocument();
    expect(screen.queryByText(/Anki Core/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Продолжить' })).not.toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls.every(([input, init]) =>
      String(input).startsWith('/api/today?') && (!init?.method || init.method === 'GET'),
    )).toBe(true);
  });

  it.each([
    [401, 'authentication required'],
    [409, 'topic subdecks are unsupported below Languages::Spanish'],
    [503, 'Service unavailable'],
  ])('does not mislabel HTTP %s as a missing deck or an Anki outage', async (status, detail) => {
    vi.mocked(fetch).mockImplementation(async () => Response.json({ detail }, { status }));
    renderToday();
    expect(await screen.findByLabelText('Не удалось загрузить план')).toBeInTheDocument();
    expect(screen.getByText(detail)).toBeInTheDocument();
    expect(screen.queryByText('Колода не найдена')).not.toBeInTheDocument();
    expect(screen.queryByText(/Anki Core/)).not.toBeInTheDocument();
  });

  it('keeps the network error explanation and clears the error after a successful retry', async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError('offline'));
    renderToday();
    expect(await screen.findByLabelText('Не удалось загрузить план')).toBeInTheDocument();
    expect(screen.getByText(/Связь с сервером потеряна/)).toBeInTheDocument();
    respondWithSummaries();
    fireEvent.click(screen.getByRole('button', { name: 'Повторить загрузку' }));
    expect(await screen.findByLabelText('План на сегодня загружен')).toBeInTheDocument();
    expect(screen.queryByText(/Связь с сервером потеряна/)).not.toBeInTheDocument();
  });
});

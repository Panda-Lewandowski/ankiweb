import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';

const today = (language: 'spanish' | 'english', total: number) => ({
  language, deck: `Languages::${language === 'spanish' ? 'Spanish' : 'English'}`,
  new: total, learning: 0, review: 0, total,
});

describe('Language Trainer', () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes('/api/today?language=spanish')) return Response.json(today('spanish', 2));
      if (path.includes('/api/today?language=english')) return Response.json(today('english', 0));
      if (path.includes('/api/review/next')) return Response.json({
        token: 'token-1', expires_in_seconds: 1200, language: 'spanish', card_id: 1, note_id: 2,
        question: { kind: 'vocabulary_production', prompt_html: 'воспользоваться возможностью',
          instruction: '', input_required: true, audio_urls: [], tts_locale: null },
      });
      if (path.includes('/check')) return Response.json({
        token: 'token-1', correct: true, diff_html: '<span>aprovechar</span>',
        back: { fields: { Answer: 'aprovechar', Example: 'Hay que aprovechar esta oportunidad.',
          OriginalError: 'incorrect source phrase' }, audio_urls: [] },
      });
      if (path.includes('/answer')) return Response.json({ answered: true, card_id: 1, rating: 'good', next: null });
      return new Response(null, { status: 500 });
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('loads Today and starts a language session', async () => {
    render(<App />);
    expect(await screen.findByText('Испанский')).toBeInTheDocument();
    expect(screen.getByText('2', { selector: '.total' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Настройки Anki' })).toHaveAttribute('href', '/settings');
    fireEvent.click(screen.getByRole('button', { name: /продолжить/i }));
    expect(await screen.findByText('воспользоваться возможностью')).toBeInTheDocument();
    expect(screen.queryByText('aprovechar')).not.toBeInTheDocument();
  });

  it('checks typed input before it enables explicit ratings', async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: /продолжить/i }));
    const input = await screen.findByRole('textbox', { name: 'Ваш ответ' });
    fireEvent.change(input, { target: { value: 'aprovechar' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(await screen.findByText('Hay que aprovechar esta oportunidad.')).toBeInTheDocument();
    expect(screen.queryByText('incorrect source phrase')).not.toBeInTheDocument();
    expect(screen.queryByText(/исходная ошибка/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /знаю/i })).toBeEnabled();
    fireEvent.keyDown(window, { key: '3' });
    expect(await screen.findByText('На сегодня всё')).toBeInTheDocument();
    const answerCall = vi.mocked(fetch).mock.calls.find(([input]) => String(input).includes('/answer'));
    expect(JSON.parse(String(answerCall?.[1]?.body))).toEqual({ rating: 'good' });
  });

  it('does not advance when the answer request fails', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes('/api/today?language=spanish')) return Response.json(today('spanish', 1));
      if (path.includes('/api/today?language=english')) return Response.json(today('english', 0));
      if (path.includes('/api/review/next')) return Response.json({
        token: 'safe-token', expires_in_seconds: 1200, language: 'spanish', card_id: 1, note_id: 2,
        question: { kind: 'vocabulary_recognition', prompt_html: 'aprovechar', instruction: '',
          input_required: false, audio_urls: [], tts_locale: null },
      });
      if (path.includes('/check')) return Response.json({ token: 'safe-token', correct: null, diff_html: null,
        back: { fields: { Translation: 'воспользоваться' }, audio_urls: [] } });
      if (path.includes('/answer')) throw new TypeError('offline');
      throw new Error('unexpected');
    });
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: /продолжить/i }));
    fireEvent.click(await screen.findByRole('button', { name: /показать ответ/i }));
    fireEvent.click(await screen.findByRole('button', { name: /знаю/i }));
    expect(await screen.findByText(/результат не подтверждён/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('воспользоваться')).toBeInTheDocument());
  });
});

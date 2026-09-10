import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { CheckResponse, ReviewCard, ReviewQuestion } from '../api/types';
import { CardAnswer } from './CardAnswer';
import { CardQuestion } from './CardQuestion';

function card(question: Partial<ReviewQuestion>): ReviewCard {
  return {
    token: 'renderer-token', expires_in_seconds: 1200, language: 'spanish',
    card_id: 1, note_id: 2,
    question: {
      kind: 'unknown', prompt_html: '', instruction: '', input_required: false,
      audio_urls: [], tts_locale: null, ...question,
    },
  };
}

describe('language card renderers', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('renders Spanish conjugation as verb, tense, person and typed input', () => {
    render(<CardQuestion card={card({
      kind: 'spanish_conjugation', prompt_html: 'hacer', input_required: true,
      instruction: 'Напиши правильную форму', tense: 'Pretérito indefinido', person: 'él/ella',
    })} value="" onChange={() => undefined} onCheck={() => undefined} busy={false} />);

    expect(screen.getByText('hacer')).toHaveClass('question__verb');
    expect(screen.getByText('Pretérito indefinido')).toBeInTheDocument();
    expect(screen.getByText('él/ella')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Ваш ответ' })).toBeInTheDocument();
  });

  it('renders each answer family explicitly and never renders OriginalError', () => {
    const result: CheckResponse = {
      token: 'renderer-token', correct: false, diff_html: '<span>have changed</span>',
      back: { fields: {
        Answer: 'have changed', Explanation: 'Plural subject → have changed',
        OriginalError: 'Our priorities has changing.',
      }, audio_urls: [] },
    };
    render(<CardAnswer result={result} kind="personal_error" typedAnswer="has changing" />);

    expect(screen.getByText('Правильный ответ')).toBeInTheDocument();
    expect(screen.getByText('Plural subject → have changed')).toBeInTheDocument();
    expect(screen.getByText('has changing')).toBeInTheDocument();
    expect(screen.queryByText('Our priorities has changing.')).not.toBeInTheDocument();
  });

  it('keeps a listening transcript hidden on the question side', () => {
    render(<CardQuestion card={card({
      kind: 'listening_dictation', input_required: true,
      instruction: 'Напечатай услышанную фразу', tts_locale: 'es_ES',
    })} value="" onChange={() => undefined} onCheck={() => undefined} busy={false} />);

    expect(screen.getByRole('button', { name: 'Прослушать аудио' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Напечатайте услышанную фразу' })).toBeInTheDocument();
    expect(screen.queryByText(/No creo que tenga razón/i)).not.toBeInTheDocument();
    expect(screen.getByText('Системный голос')).toBeInTheDocument();
  });
});

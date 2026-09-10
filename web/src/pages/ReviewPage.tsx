import { Archive, ArrowLeft, PauseCircle } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import type { CheckResponse, Language, Rating, ReviewCard } from '../api/types';
import { Button } from '../components/ui/button';
import { CardAnswer } from '../review/CardAnswer';
import { CardQuestion } from '../review/CardQuestion';

const LANGUAGE = { spanish: 'Испанский', english: 'Английский' } as const;
const KIND: Record<string, string> = {
  vocabulary_production: 'Активная лексика', vocabulary_recognition: 'Узнавание',
  grammar_cloze: 'Грамматика', personal_error: 'Личная ошибка',
  spanish_conjugation: 'Спряжение', listening_dictation: 'Диктант',
  listening_comprehension: 'Аудирование', phrase_retrieval: 'Готовая фраза', unknown: 'Повторение',
};
const RATINGS: { value: Rating; label: string; key: string }[] = [
  { value: 'again', label: 'Не вспомнила', key: '1' },
  { value: 'hard', label: 'С трудом', key: '2' },
  { value: 'good', label: 'Знаю', key: '3' },
  { value: 'easy', label: 'Мгновенно', key: '4' },
];

type Props = { language: Language; initialTotal: number; onExit: () => void };

export function ReviewPage({ language, initialTotal, onExit }: Props) {
  const [card, setCard] = useState<ReviewCard | null>(null);
  const [checked, setChecked] = useState<CheckResponse | null>(null);
  const [typed, setTyped] = useState('');
  const [completed, setCompleted] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [finished, setFinished] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const next = await api.next(language);
      setCard(next);
      setFinished(next === null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось получить карточку.');
    } finally {
      setLoading(false);
    }
  }, [language]);

  useEffect(() => { void load(); }, [load]);

  const check = useCallback(async () => {
    if (!card || busy) return;
    setBusy(true);
    setError('');
    try {
      setChecked(await api.check(card.token, typed));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Проверка не выполнена.');
    } finally {
      setBusy(false);
    }
  }, [busy, card, typed]);

  const rate = useCallback(async (rating: Rating) => {
    if (!card || !checked || busy) return;
    setBusy(true);
    setError('');
    try {
      const result = await api.answer(card.token, rating);
      setCompleted((value) => value + 1);
      setCard(result.next);
      setChecked(null);
      setTyped('');
      setFinished(result.next === null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Ответ не был засчитан.');
    } finally {
      setBusy(false);
    }
  }, [busy, card, checked]);

  useEffect(() => {
    function keyboard(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      const typing = target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA';
      if (checked) {
        const rating = RATINGS.find((item) => item.key === event.key)?.value;
        if (rating) { event.preventDefault(); void rate(rating); }
      } else if (!typing && (event.key === ' ' || event.key === 'Enter')) {
        event.preventDefault(); void check();
      } else if (event.key === 'Escape') onExit();
    }
    window.addEventListener('keydown', keyboard);
    return () => window.removeEventListener('keydown', keyboard);
  }, [check, checked, onExit, rate]);

  const position = Math.min(completed + (card ? 1 : 0), initialTotal);
  const progress = initialTotal ? Math.min(100, (completed / initialTotal) * 100) : 0;
  const title = useMemo(() => KIND[card?.question.kind ?? 'unknown'] ?? 'Повторение', [card]);
  const detail = card?.question.topic
    ? card.question.topic.replaceAll('_', ' ')
    : card?.question.cefr ?? '';

  async function cardAction(action: 'suspend' | 'bury') {
    if (!card || busy) return;
    setBusy(true);
    setError('');
    try {
      await api[action](card.card_id);
      setCompleted((value) => value + 1);
      setChecked(null); setTyped('');
      const next = await api.next(language);
      setCard(next); setFinished(next === null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Действие не выполнено.');
    } finally { setBusy(false); }
  }

  return (
    <main className="review shell-page">
      <header className="review__header">
        <Button size="icon" variant="quiet" onClick={onExit} aria-label="Вернуться на сегодня">
          <ArrowLeft size={20} />
        </Button>
        <div className="review__identity">
          <strong>{LANGUAGE[language]}</strong>
          <span>{[title, detail].filter(Boolean).join(' · ')}</span>
        </div>
        <div className="review__position numeric" aria-label={`Карточка ${position} из ${initialTotal}`}>
          {position} / {initialTotal}
        </div>
      </header>
      <div className="progress-track" aria-hidden="true"><span style={{ width: `${progress}%` }} /></div>

      <div className="notice-slot" aria-live="assertive">
        {error ? <div className="error-notice"><span>{error}</span></div> : null}
      </div>

      <section className="review-card" aria-busy={loading || busy}>
        {loading ? <div className="review-skeleton" aria-label="Загрузка карточки" /> : null}
        {!loading && finished ? (
          <div className="finished-state">
            <span>Готово</span>
            <h1>На сегодня всё</h1>
            <p>Повторения записаны в Anki.</p>
            <Button onClick={onExit}>Вернуться</Button>
          </div>
        ) : null}
        {!loading && card ? (
          <>
            <div className="review-card__actions">
              <Button size="icon" variant="quiet" onClick={() => void cardAction('bury')}
                aria-label="Отложить карточку" title="Отложить карточку">
                <Archive size={18} />
              </Button>
              <Button size="icon" variant="quiet" onClick={() => void cardAction('suspend')}
                aria-label="Приостановить карточку" title="Приостановить карточку">
                <PauseCircle size={18} />
              </Button>
            </div>
            {!checked ? (
              <CardQuestion card={card} value={typed} onChange={setTyped} onCheck={() => void check()} busy={busy} />
            ) : (
              <CardAnswer result={checked} kind={card.question.kind} typedAnswer={typed} />
            )}
          </>
        ) : null}
      </section>

      <div className={`rating-dock ${checked ? 'rating-dock--visible' : ''}`} aria-hidden={!checked}>
        {RATINGS.map((rating) => (
          <button key={rating.value} className={`rating rating--${rating.value}`}
            disabled={!checked || busy} onClick={() => void rate(rating.value)}>
            <kbd>{rating.key}</kbd><span>{rating.label}</span>
          </button>
        ))}
      </div>
    </main>
  );
}

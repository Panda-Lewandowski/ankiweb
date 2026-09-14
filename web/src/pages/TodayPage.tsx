import { ArrowRight, BookOpen, BookPlus, CheckCircle2, CircleAlert, LoaderCircle, RefreshCw, Settings } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { Language, TodaySummary } from '../api/types';
import { GlassPanel } from '../components/GlassPanel';
import { Button } from '../components/ui/button';

const META: Record<Language, { name: string; code: string; deck: string }> = {
  spanish: { name: 'Испанский', code: 'ES', deck: 'Languages::Spanish' },
  english: { name: 'Английский', code: 'EN', deck: 'Languages::English' },
};

function describeLoadError(reason: unknown) {
  // A missing deck is a collection-setup error, not a failed Anki health probe.
  const missingDeck = reason instanceof ApiError && reason.status === 409
    ? Object.values(META).find(({ deck }) => reason.message === `required deck does not exist: ${deck}`)
    : undefined;
  return missingDeck
    ? {
      label: 'Колода не найдена',
      message: `В серверной коллекции не найдена колода «${missingDeck.name}» (${missingDeck.deck}).`,
    }
    : {
      label: 'Не удалось загрузить план',
      message: reason instanceof Error ? reason.message : 'Не удалось загрузить план на сегодня.',
    };
}

function greeting() {
  const hour = new Date().getHours();
  if (hour < 6) return 'Доброй ночи';
  if (hour < 12) return 'Доброе утро';
  if (hour < 18) return 'Добрый день';
  return 'Добрый вечер';
}

export function TodayPage({
  onStart,
  onImport,
}: {
  onStart: (language: Language, total: number) => void;
  onImport: () => void;
}) {
  const [summaries, setSummaries] = useState<Partial<Record<Language, TodaySummary>>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ReturnType<typeof describeLoadError> | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [spanish, english] = await Promise.all([api.today('spanish'), api.today('english')]);
      setSummaries({ spanish, english });
    } catch (reason) {
      setError(describeLoadError(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const planStatus = error
    ? { label: error.label, className: 'connection-chip connection-chip--error', Icon: CircleAlert }
    : loading
      ? { label: 'Загрузка плана на сегодня', className: 'connection-chip connection-chip--loading', Icon: LoaderCircle }
      : { label: 'План на сегодня загружен', className: 'connection-chip', Icon: CheckCircle2 };

  return (
    <main className="today shell-page">
      <header className="today__header">
        <div>
          <p className="eyebrow">Language Trainer</p>
          <h1>{greeting()}</h1>
          <p>Небольшая практика, которую действительно стоит запомнить.</p>
        </div>
        <div className="today__actions">
          <div className={planStatus.className} aria-label={planStatus.label}>
            <planStatus.Icon size={14} className={loading ? 'spin' : undefined} aria-hidden="true" />
            <span>{planStatus.label}</span>
          </div>
          <Button size="icon" variant="quiet" onClick={onImport} aria-label="Импорт урока">
            <BookPlus size={17} aria-hidden="true" />
          </Button>
          {import.meta.env.VITE_HIDE_LEGACY !== 'true' ? <Button asChild size="icon" variant="quiet">
            <a href="/settings" aria-label="Настройки Anki">
              <Settings size={17} aria-hidden="true" />
            </a>
          </Button> : null}
        </div>
      </header>

      <div className="notice-slot" aria-live="polite">
        {error ? (
          <div className="error-notice">
            <span>{error.message}</span>
            <Button size="icon" variant="quiet" onClick={() => void load()} aria-label="Повторить загрузку">
              <RefreshCw size={17} />
            </Button>
          </div>
        ) : null}
      </div>

      <div className="language-grid" aria-busy={loading}>
        {(['spanish', 'english'] as const).map((language) => {
          const summary = summaries[language];
          return (
            <GlassPanel className="language-panel" key={language}>
              <div className="language-panel__top">
                <span className="language-code">{META[language].code}</span>
                <BookOpen size={19} aria-hidden="true" />
              </div>
              <h2>{META[language].name}</h2>
              {loading ? (
                <div className="summary-skeleton" aria-label="Загрузка" />
              ) : summary ? (
                <>
                  <p className="total numeric">{summary.total}</p>
                  <p className="total-label">карточек сегодня</p>
                  <div className="count-row">
                    <span><strong className="numeric">{summary.review}</strong> повторить</span>
                    <span><strong className="numeric">{summary.learning}</strong> учить</span>
                    <span><strong className="numeric">{summary.new}</strong> новых</span>
                  </div>
                  <Button disabled={summary.total === 0} onClick={() => onStart(language, summary.total)}>
                    {summary.total ? 'Продолжить' : 'На сегодня всё'}
                    {summary.total ? <ArrowRight size={17} aria-hidden="true" /> : <CheckCircle2 size={17} />}
                  </Button>
                </>
              ) : (
                <p className="empty-state">Нет данных по этой колоде.</p>
              )}
            </GlassPanel>
          );
        })}
      </div>

      <footer className="today__footer">Только нужные карточки · расписание ведёт Anki · <a href="/about">О проекте и исходный код</a></footer>
    </main>
  );
}

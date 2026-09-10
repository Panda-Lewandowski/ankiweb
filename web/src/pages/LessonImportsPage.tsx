import {
  AlertTriangle, ArrowLeft, Check, ClipboardCheck, Eye, History, LoaderCircle, RefreshCw,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import type {
  ImportDecision, Language, LessonBatchPayload, LessonCardInput, LessonImportResult,
  LessonReceipt, LessonSource,
} from '../api/types';
import { GlassPanel } from '../components/GlassPanel';
import { Button } from '../components/ui/button';

const EXAMPLE_CARDS: LessonCardInput[] = [
  {
    type: 'personal_error',
    prompt: 'No creo que ella ___ razón. (tener)',
    answer: 'tenga',
    explanation: 'No creo que + subjuntivo',
    cefr: 'B1',
    topic: 'subjuntivo',
  },
  {
    type: 'listening_dictation',
    sentence: 'No creo que tenga razón.',
    translation: 'Не думаю, что она права.',
    note: 'Subjuntivo after no creo que',
    cefr: 'B1',
    topic: 'subjuntivo',
  },
];

const STATUS: Record<ImportDecision['status'], { label: string; tone: string }> = {
  would_add: { label: 'Будет добавлена', tone: 'success' },
  added: { label: 'Добавлена', tone: 'success' },
  would_update_metadata: { label: 'Будут обновлены метаданные', tone: 'info' },
  updated_metadata: { label: 'Метаданные обновлены', tone: 'info' },
  skipped_duplicate: { label: 'Дубликат пропущен', tone: 'muted' },
  ambiguous_duplicate: { label: 'Нужна ручная проверка', tone: 'warning' },
  metadata_conflict: { label: 'Конфликт метаданных', tone: 'warning' },
  error: { label: 'Ошибка', tone: 'error' },
};

const SOURCE_LABELS: Record<LessonSource, string> = {
  chatgpt_lesson: 'Урок ChatGPT',
  teacher_lesson: 'Урок с преподавателем',
  speaking: 'Разговорная практика',
  writing: 'Письмо',
  listening: 'Аудирование',
};

function localDate() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60_000).toISOString().slice(0, 10);
}

function cardsFromJson(value: string): LessonCardInput[] {
  const parsed: unknown = JSON.parse(value);
  if (!Array.isArray(parsed) || parsed.length === 0) {
    throw new Error('JSON должен содержать непустой массив карточек.');
  }
  if (!parsed.every((item) => item && typeof item === 'object' && !Array.isArray(item))) {
    throw new Error('Каждая карточка должна быть JSON-объектом.');
  }
  return parsed as LessonCardInput[];
}

function countActionable(result: LessonImportResult | null) {
  if (!result || result.mode !== 'preview') return 0;
  return result.would_add + result.would_update_metadata;
}

function summaryText(summary: LessonImportResult['summary']) {
  const added = summary.added || summary.would_add;
  const updated = summary.updated_metadata || summary.would_update_metadata;
  return `${added} новых · ${updated} обновлений · ${summary.duplicates} дубликатов`;
}

export function LessonImportsPage({ onBack }: { onBack: () => void }) {
  const [language, setLanguage] = useState<Language>('spanish');
  const [source, setSource] = useState<LessonSource>('chatgpt_lesson');
  const [date, setDate] = useState(localDate);
  const [cardsJson, setCardsJson] = useState(() => JSON.stringify(EXAMPLE_CARDS, null, 2));
  const [preview, setPreview] = useState<LessonImportResult | null>(null);
  const [receipts, setReceipts] = useState<LessonReceipt[]>([]);
  const [busy, setBusy] = useState<'preview' | 'commit' | ''>('');
  const [error, setError] = useState('');

  const loadReceipts = useCallback(async () => {
    try {
      setReceipts(await api.lessonReceipts());
    } catch {
      // Receipt history is secondary; the import form remains usable.
    }
  }, []);

  useEffect(() => { void loadReceipts(); }, [loadReceipts]);

  const payload = useMemo<LessonBatchPayload | null>(() => {
    try {
      return { lesson: { language, source, date }, cards: cardsFromJson(cardsJson) };
    } catch {
      return null;
    }
  }, [cardsJson, date, language, source]);

  const invalidatePreview = () => {
    setPreview(null);
    setError('');
  };

  const runPreview = async () => {
    let nextPayload: LessonBatchPayload;
    try {
      nextPayload = { lesson: { language, source, date }, cards: cardsFromJson(cardsJson) };
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Некорректный JSON.');
      return;
    }
    setBusy('preview');
    setError('');
    try {
      setPreview(await api.previewLesson(nextPayload));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось проверить карточки.');
    } finally {
      setBusy('');
    }
  };

  const commit = async () => {
    if (!payload || !preview) return;
    setBusy('commit');
    setError('');
    try {
      setPreview(await api.importLesson(payload));
      await loadReceipts();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Импорт не был подтверждён сервером.');
    } finally {
      setBusy('');
    }
  };

  const actionable = countActionable(preview);

  return (
    <main className="imports shell-page">
      <header className="imports__header">
        <Button size="icon" variant="quiet" onClick={onBack} aria-label="Назад на сегодня">
          <ArrowLeft size={18} />
        </Button>
        <div>
          <p className="eyebrow">Lesson Imports</p>
          <h1>Карточки из урока</h1>
          <p>Сначала проверьте план. Preview ничего не меняет в Anki.</p>
        </div>
      </header>

      <div className="imports__grid">
        <GlassPanel className="import-form-panel" title="Новый импорт">
          <div className="import-context">
            <label>
              <span>Язык</span>
              <select value={language} onChange={(event) => {
                setLanguage(event.target.value as Language);
                invalidatePreview();
              }}>
                <option value="spanish">Испанский</option>
                <option value="english">Английский</option>
              </select>
            </label>
            <label>
              <span>Источник</span>
              <select value={source} onChange={(event) => {
                setSource(event.target.value as LessonSource);
                invalidatePreview();
              }}>
                {Object.entries(SOURCE_LABELS).map(([value, label]) => (
                  <option value={value} key={value}>{label}</option>
                ))}
              </select>
            </label>
            <label>
              <span>Дата</span>
              <input type="date" value={date} onChange={(event) => {
                setDate(event.target.value);
                invalidatePreview();
              }} />
            </label>
          </div>

          <label className="json-field">
            <span>Карточки JSON</span>
            <textarea
              aria-label="Карточки JSON"
              spellCheck={false}
              value={cardsJson}
              onChange={(event) => {
                setCardsJson(event.target.value);
                invalidatePreview();
              }}
            />
          </label>
          <p className="import-hint">Оптимально 5–10 карточек: реальные ошибки, провалы в извлечении и полезные chunks.</p>
          {error ? <div className="error-notice import-error" role="alert">{error}</div> : null}
          <Button onClick={() => void runPreview()} disabled={Boolean(busy)}>
            {busy === 'preview' ? <LoaderCircle className="spin" size={17} /> : <Eye size={17} />}
            Проверить без изменений
          </Button>
        </GlassPanel>

        <div className="imports__side">
          <GlassPanel className="preview-panel" title="Результат проверки">
            {!preview ? (
              <div className="preview-empty">
                <ClipboardCheck size={24} />
                <p>Здесь появятся точные решения: добавить, обновить метаданные или пропустить.</p>
              </div>
            ) : (
              <>
                <div className="import-summary">
                  <strong>{preview.mode === 'commit' ? 'Импорт завершён' : 'План готов'}</strong>
                  <span>{summaryText(preview.summary)}</span>
                  {preview.receipt_id ? <small>Receipt · {preview.receipt_id.slice(0, 10)}</small> : null}
                </div>
                <div className="decision-list">
                  {preview.items.map((item) => {
                    const status = STATUS[item.status];
                    return (
                      <div className="decision" key={`${item.index}-${item.status}`}>
                        <span className={`decision__status decision__status--${status.tone}`}>{status.label}</span>
                        <strong>#{item.index} · {item.model ?? item.type ?? 'Карточка'}</strong>
                        <small>{item.deck ?? item.message}</small>
                        {item.filled_fields?.length ? <small>Поля: {item.filled_fields.join(', ')}</small> : null}
                        {item.message && item.deck ? <small className="decision__error">{item.message}</small> : null}
                      </div>
                    );
                  })}
                </div>
                {preview.error_count ? (
                  <div className="import-warning">
                    <AlertTriangle size={16} />
                    {preview.error_count} карточек требуют исправления и не будут изменены.
                  </div>
                ) : null}
                {preview.mode === 'preview' ? (
                  <Button onClick={() => void commit()} disabled={Boolean(busy) || actionable === 0}>
                    {busy === 'commit' ? <LoaderCircle className="spin" size={17} /> : <Check size={17} />}
                    Импортировать {actionable || ''}
                  </Button>
                ) : null}
              </>
            )}
          </GlassPanel>

          <GlassPanel className="receipt-panel" title="Последние импорты">
            <div className="receipt-title"><History size={16} /><span>Без текста карточек</span></div>
            {receipts.length ? receipts.slice(0, 5).map((receipt) => (
              <div className="receipt-row" key={receipt.receipt_id}>
                <div><strong>{receipt.lesson.language === 'spanish' ? 'Испанский' : 'Английский'}</strong><span>{SOURCE_LABELS[receipt.lesson.source]}</span></div>
                <div><strong>+{receipt.summary.added}</strong><span>{receipt.lesson.date}</span></div>
              </div>
            )) : (
              <p className="receipt-empty">Подтверждённых импортов пока нет.</p>
            )}
            <Button variant="quiet" onClick={() => void loadReceipts()} aria-label="Обновить историю импортов">
              <RefreshCw size={15} /> Обновить
            </Button>
          </GlassPanel>
        </div>
      </div>
    </main>
  );
}

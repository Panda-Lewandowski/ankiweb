import DOMPurify from 'dompurify';
import type { CheckResponse } from '../api/types';

const LABELS: Record<string, string> = {
  Answer: 'Ответ', Example: 'Пример', Translation: 'Перевод', Explanation: 'Почему',
  Text: 'Фраза', BackExtra: 'Пояснение', Sentence: 'Фраза', Note: 'Заметка',
  answer_html: 'Ответ',
};

export function CardAnswer({ result }: { result: CheckResponse }) {
  const fields = Object.entries(result.back.fields)
    .filter(([name, value]) => name !== 'OriginalError' && value.trim());
  return (
    <div className="answer" aria-live="polite">
      {result.correct !== null ? (
        <div className={`answer__status ${result.correct ? 'answer__status--correct' : 'answer__status--retry'}`}>
          {result.correct ? 'Точно' : 'Сверь ответ'}
        </div>
      ) : null}
      {result.diff_html ? (
        <div className="answer__diff"
          dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(result.diff_html) }} />
      ) : null}
      <div className="answer__fields">
        {fields.map(([name, value], index) => (
          <div className={index === 0 ? 'answer-field answer-field--primary' : 'answer-field'} key={name}>
            <span>{LABELS[name] ?? name}</span>
            <div dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(value) }} />
          </div>
        ))}
      </div>
    </div>
  );
}

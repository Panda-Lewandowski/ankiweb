import DOMPurify from 'dompurify';
import type { CheckResponse } from '../api/types';
import { AnswerFields } from './renderers/AnswerRenderers';

export function CardAnswer({ result, kind, typedAnswer }: {
  result: CheckResponse; kind: string; typedAnswer: string;
}) {
  return (
    <div className={`answer answer--${kind}`} aria-live="polite">
      {result.correct !== null ? (
        <div className={`answer__status ${result.correct ? 'answer__status--correct' : 'answer__status--retry'}`}>
          {result.correct ? 'Точно' : 'Сверь ответ'}
        </div>
      ) : null}
      {result.correct !== null && typedAnswer.trim() ? (
        <div className="answer__typed"><span>Ваш ответ</span><strong>{typedAnswer}</strong></div>
      ) : null}
      {result.diff_html ? (
        <div className="answer__diff"
          dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(result.diff_html) }} />
      ) : null}
      <div className="answer__fields">
        <AnswerFields kind={kind} fields={result.back.fields} />
      </div>
    </div>
  );
}

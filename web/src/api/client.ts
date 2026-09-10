import type { AnswerResponse, CheckResponse, Language, Rating, ReviewCard, TodaySummary } from './types';

export class ApiError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = 'ApiError';
  }
}

function clientId(): string {
  const key = 'language-trainer-client';
  let value = sessionStorage.getItem(key);
  if (!value) {
    value = crypto.randomUUID();
    sessionStorage.setItem(key, value);
  }
  return value;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      credentials: 'include',
      ...init,
      headers: {
        'X-Review-Client': clientId(),
        ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
        ...init?.headers,
      },
    });
  } catch {
    throw new ApiError('Связь с сервером потеряна. Результат не подтверждён; интерфейс не изменён.');
  }
  if (response.status === 204) return null as T;
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null;
    throw new ApiError(body?.detail ?? 'Не удалось выполнить запрос.', response.status);
  }
  return response.json() as Promise<T>;
}

async function requestAudio(path: string): Promise<Blob> {
  let response: Response;
  try {
    response = await fetch(path, {
      credentials: 'include',
      headers: { 'X-Review-Client': clientId() },
    });
  } catch {
    throw new ApiError('Не удалось получить аудио: связь с сервером потеряна.');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null;
    throw new ApiError(body?.detail ?? 'Не удалось получить аудио.', response.status);
  }
  return response.blob();
}

export const api = {
  today: (language: Language) => request<TodaySummary>(`/api/today?language=${language}`),
  next: (language: Language) => request<ReviewCard | null>(`/api/review/next?language=${language}`),
  check: (token: string, typedAnswer: string) => request<CheckResponse>(
    `/api/review/${encodeURIComponent(token)}/check`,
    { method: 'POST', body: JSON.stringify({ typed_answer: typedAnswer }) },
  ),
  audio: (token: string) => requestAudio(`/api/review/${encodeURIComponent(token)}/audio`),
  answer: (token: string, rating: Rating) => request<AnswerResponse>(
    `/api/review/${encodeURIComponent(token)}/answer`,
    { method: 'POST', body: JSON.stringify({ rating }) },
  ),
  suspend: (cardId: number) => request(`/api/cards/${cardId}/suspend`, { method: 'POST' }),
  bury: (cardId: number) => request(`/api/cards/${cardId}/bury`, { method: 'POST' }),
};

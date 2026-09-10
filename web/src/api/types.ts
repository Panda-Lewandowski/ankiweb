export type Language = 'spanish' | 'english';
export type Rating = 'again' | 'hard' | 'good' | 'easy';

export type TodaySummary = {
  language: Language;
  deck: string;
  new: number;
  learning: number;
  review: number;
  total: number;
};

export type ReviewQuestion = {
  kind: string;
  prompt_html: string;
  instruction: string;
  input_required: boolean;
  audio_urls: string[];
  tts_locale: string | null;
  topic?: string;
  cefr?: string;
  tense?: string;
  person?: string;
};

export type ReviewCard = {
  token: string;
  expires_in_seconds: number;
  language: Language;
  card_id: number;
  note_id: number;
  question: ReviewQuestion;
};

export type CheckResponse = {
  token: string;
  correct: boolean | null;
  diff_html: string | null;
  back: { fields: Record<string, string>; audio_urls: string[] };
};

export type AnswerResponse = {
  answered: true;
  card_id: number;
  rating: Rating;
  next: ReviewCard | null;
};

export type LessonSource =
  | 'chatgpt_lesson'
  | 'teacher_lesson'
  | 'speaking'
  | 'writing'
  | 'listening';

export type LessonCardInput = {
  type: string;
  cefr: 'B1' | 'B2' | 'C1';
  topic: string;
  [field: string]: string;
};

export type LessonBatchPayload = {
  lesson: { language: Language; source: LessonSource; date: string };
  cards: LessonCardInput[];
};

export type ImportDecision = {
  index: number;
  status:
    | 'would_add'
    | 'added'
    | 'would_update_metadata'
    | 'updated_metadata'
    | 'skipped_duplicate'
    | 'ambiguous_duplicate'
    | 'metadata_conflict'
    | 'error';
  model?: string;
  deck?: string;
  type?: string;
  note_id?: number;
  matching_note_ids?: number[];
  matching_batch_index?: number;
  conflicting_fields?: string[];
  filled_fields?: string[];
  added_tags?: string[];
  message?: string;
};

export type ImportSummary = {
  added: number;
  would_add: number;
  updated_metadata: number;
  would_update_metadata: number;
  duplicates: number;
  error_count: number;
};

export type LessonImportResult = ImportSummary & {
  mode: 'preview' | 'commit';
  lesson: LessonBatchPayload['lesson'];
  summary: ImportSummary;
  items: ImportDecision[];
  errors: { index: number; message: string }[];
  receipt_id: string | null;
  created_at?: string;
};

export type LessonReceipt = {
  receipt_id: string;
  created_at: string;
  mode: 'commit';
  lesson: LessonBatchPayload['lesson'];
  summary: ImportSummary;
  items: ImportDecision[];
  errors: { index: number; message: string }[];
};

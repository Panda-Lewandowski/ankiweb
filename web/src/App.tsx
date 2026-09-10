import { useState } from 'react';
import type { Language } from './api/types';
import { ReviewPage } from './pages/ReviewPage';
import { TodayPage } from './pages/TodayPage';

type Session = { language: Language; total: number } | null;

export default function App() {
  const [session, setSession] = useState<Session>(null);
  return (
    <div className="app-shell">
      <div className="app-background" aria-hidden="true" />
      <div className="app-overlay" aria-hidden="true" />
      <div className="app-content">
        {session ? (
          <ReviewPage language={session.language} initialTotal={session.total} onExit={() => setSession(null)} />
        ) : (
          <TodayPage onStart={(language, total) => setSession({ language, total })} />
        )}
      </div>
    </div>
  );
}

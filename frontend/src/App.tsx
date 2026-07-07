import React, { Suspense, lazy } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Sidebar from './components/layout/Sidebar';
import './index.css';

// Lazy-loaded pages for code splitting
const ChatPage        = lazy(() => import('./pages/ChatPage'));
const GraphViewerPage = lazy(() => import('./pages/GraphViewerPage'));
const PaperViewerPage = lazy(() => import('./pages/PaperViewerPage'));
const EvaluationPage  = lazy(() => import('./pages/EvaluationPage'));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 2,
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
});

const PageLoader: React.FC = () => (
  <div style={{
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    height: '100vh',
    flexDirection: 'column',
    gap: '16px',
    background: 'var(--color-bg-primary)',
    flex: 1,
  }}>
    <div style={{
      width: '40px', height: '40px', borderRadius: '50%',
      border: '3px solid var(--color-bg-tertiary)',
      borderTopColor: 'var(--color-accent-blue)',
      animation: 'spin 0.8s linear infinite',
    }} />
    <p style={{ color: 'var(--color-text-muted)', fontSize: '0.875rem' }}>Loading…</p>
  </div>
);

const AppLayout: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div style={{ display: 'flex', width: '100%', minHeight: '100vh' }}>
    <Sidebar />
    <main style={{ flex: 1, minWidth: 0, overflow: 'hidden' }}>
      {children}
    </main>
  </div>
);

const App: React.FC = () => {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppLayout>
          <Suspense fallback={<PageLoader />}>
            <Routes>
              <Route path="/"                  element={<ChatPage />} />
              <Route path="/graph"             element={<GraphViewerPage />} />
              <Route path="/papers/:paperId"   element={<PaperViewerPage />} />
              <Route path="/evaluation"        element={<EvaluationPage />} />
              {/* Fallback */}
              <Route path="*"                  element={<ChatPage />} />
            </Routes>
          </Suspense>
        </AppLayout>
      </BrowserRouter>
    </QueryClientProvider>
  );
};

export default App;

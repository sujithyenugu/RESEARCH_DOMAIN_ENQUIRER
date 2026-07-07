import React, { useState, useEffect, useRef, useCallback } from 'react';
import QueryInput from '../components/chat/QueryInput';
import AnswerCard from '../components/chat/AnswerCard';
import CitationList from '../components/chat/CitationList';
import ChunkViewer from '../components/chat/ChunkViewer';
import { useChatStore } from '../store';
import { submitQuery } from '../services/api';
import { mockStream } from '../services/websocket';
import type { QueryFilters, QueryResponse } from '../types';
import './ChatPage.css';

const SUGGESTED_QUERIES = [
  'How does LoRA compare to full fine-tuning on LLMs?',
  'What is the RLHF training process for ChatGPT?',
  'Explain the differences between RAG and fine-tuning for knowledge injection',
  'How do vision transformers compare to CNNs on ImageNet?',
];

const ChatPage: React.FC = () => {
  const {
    sessions, activeSessionId, isStreaming, streamingText, pendingResponse,
    filters, createSession, setActiveSession, addUserMessage,
    setStreaming, appendStreamToken, finalizeStream, resetStream, updateFilters,
  } = useChatStore();

  const [currentResponse, setCurrentResponse] = useState<QueryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const cancelStreamRef = useRef<(() => void) | null>(null);

  const activeSession = sessions.find(s => s.id === (activeSessionId ?? sessions[0]?.id));

  useEffect(() => {
    if (!activeSessionId && sessions.length > 0) {
      setActiveSession(sessions[0].id);
    }
  }, [activeSessionId, sessions, setActiveSession]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [activeSession?.messages, streamingText]);

  useEffect(() => {
    if (pendingResponse) {
      setCurrentResponse(pendingResponse);
    }
  }, [pendingResponse]);

  const handleQuery = useCallback(async (query: string, queryFilters: QueryFilters) => {
    if (!activeSession) return;
    setError(null);
    setCurrentResponse(null);

    addUserMessage(activeSession.id, query);
    setStreaming(true);

    try {
      const response = await submitQuery({
        query,
        topK: queryFilters.topK,
        dateFrom: queryFilters.dateFrom,
        dateTo: queryFilters.dateTo,
        categories: queryFilters.categories,
      });

      // Start mock streaming
      cancelStreamRef.current = mockStream(
        response,
        (token) => appendStreamToken(token),
        (fullResponse) => finalizeStream(activeSession.id, fullResponse),
      );
    } catch (err) {
      setStreaming(false);
      setError('Failed to get a response. Please try again.');
      console.error('[Chat] Query error:', err);
    }
  }, [activeSession, addUserMessage, setStreaming, appendStreamToken, finalizeStream]);

  const handleNewChat = () => {
    cancelStreamRef.current?.();
    resetStream();
    setCurrentResponse(null);
    createSession();
  };

  const activeMessages = activeSession?.messages ?? [];
  const lastAssistantMessage = [...activeMessages].reverse().find(m => m.role === 'assistant');
  const displayResponse = currentResponse ?? lastAssistantMessage?.response ?? null;

  return (
    <div className="chat-page">
      {/* Session sidebar */}
      <aside className="chat-sessions">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
          <span className="section-label">Sessions</span>
          <button className="btn btn-primary" onClick={handleNewChat} style={{ padding: '5px 12px', fontSize: '0.8rem' }}>
            + New
          </button>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', overflowY: 'auto' }}>
          {sessions.map(session => (
            <button
              key={session.id}
              onClick={() => setActiveSession(session.id)}
              className={`session-item ${session.id === activeSessionId ? 'session-item--active' : ''}`}
            >
              <span style={{ fontSize: '0.85rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {session.title.length > 32 ? session.title.slice(0, 31) + '…' : session.title}
              </span>
              <span style={{ fontSize: '0.7rem', color: 'var(--color-text-muted)', flexShrink: 0 }}>
                {new Date(session.updatedAt).toLocaleDateString()}
              </span>
            </button>
          ))}
        </div>
      </aside>

      {/* Main chat area */}
      <main className="chat-main">
        {/* Query input */}
        <div className="chat-input-area">
          <QueryInput
            onSubmit={handleQuery}
            disabled={isStreaming}
            filters={filters}
            onFiltersChange={updateFilters}
          />
        </div>

        {/* Conversation */}
        <div className="chat-conversation scroll-panel">
          {activeMessages.length === 0 && !isStreaming && (
            <div className="chat-welcome anim-fade-in">
              <div style={{ fontSize: '3rem', marginBottom: '12px' }}>🔬</div>
              <h2 className="gradient-text" style={{ marginBottom: '8px' }}>Research Domain Enquirer</h2>
              <p style={{ color: 'var(--color-text-muted)', marginBottom: '24px', maxWidth: '480px', textAlign: 'center' }}>
                Ask any research question and get RAG-grounded answers with citations, evidence chunks, and hallucination verification.
              </p>
              <div className="suggestions stagger">
                {SUGGESTED_QUERIES.map((q, i) => (
                  <button
                    key={i}
                    className="suggestion-btn anim-fade-in"
                    onClick={() => handleQuery(q, filters)}
                    disabled={isStreaming}
                    style={{ animationDelay: `${i * 80}ms` }}
                  >
                    <span style={{ color: 'var(--color-accent-blue)', marginRight: '8px' }}>→</span>
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Message history */}
          {activeMessages.map(msg => (
            <div
              key={msg.id}
              className={`chat-message chat-message--${msg.role} anim-fade-in`}
            >
              {msg.role === 'user' ? (
                <div className="user-message glass-card">
                  <span style={{ fontSize: '0.85rem', fontWeight: 500, color: 'var(--color-text-primary)' }}>
                    {msg.content}
                  </span>
                </div>
              ) : msg.response ? (
                <AnswerCard response={msg.response} />
              ) : null}
            </div>
          ))}

          {/* Streaming answer */}
          {isStreaming && (
            <div className="chat-message chat-message--assistant anim-fade-in">
              <AnswerCard
                response={{
                  queryId: 'streaming',
                  query: '',
                  answer: streamingText,
                  confidence: 0,
                  confidenceLevel: 'high',
                  action: 'PASS',
                  citations: [],
                  chunks: [],
                  verification: {
                    overallConfidence: 0,
                    action: 'PASS',
                    claimsExtracted: 0,
                    supported: 0,
                    partiallySupported: 0,
                    unsupported: 0,
                    contradicted: 0,
                    evidenceCoverage: 0,
                    citationAccuracy: 0,
                    claims: [],
                  },
                  latencyMs: 0,
                  timestamp: new Date().toISOString(),
                }}
                streamingText={streamingText}
                isStreaming
              />
            </div>
          )}

          {/* Error */}
          {error && (
            <div className="glass-card anim-fade-in" style={{ padding: '14px', borderColor: 'rgba(239,68,68,0.3)', marginTop: '12px' }}>
              <p style={{ color: 'var(--color-accent-red)', fontSize: '0.875rem' }}>⚠️ {error}</p>
            </div>
          )}

          <div ref={chatEndRef} />
        </div>
      </main>

      {/* Right panel: citations + chunks */}
      <aside className="chat-right-panel scroll-panel">
        {isStreaming && !displayResponse && (
          <div style={{ padding: '20px', textAlign: 'center' }}>
            <div className="anim-pulse" style={{ color: 'var(--color-accent-blue)', fontSize: '1.5rem' }}>⟳</div>
            <p style={{ fontSize: '0.8rem', color: 'var(--color-text-muted)', marginTop: '8px' }}>
              Retrieving evidence…
            </p>
          </div>
        )}

        {displayResponse && (
          <>
            <div style={{ marginBottom: '20px' }}>
              <CitationList citations={displayResponse.citations} />
            </div>
            <ChunkViewer chunks={displayResponse.chunks} />
          </>
        )}

        {!displayResponse && !isStreaming && (
          <div style={{ padding: '24px', textAlign: 'center' }}>
            <div style={{ fontSize: '2rem', opacity: 0.2, marginBottom: '10px' }}>📚</div>
            <p style={{ fontSize: '0.8rem', color: 'var(--color-text-muted)' }}>
              Citations and evidence chunks will appear here
            </p>
          </div>
        )}
      </aside>
    </div>
  );
};

export default ChatPage;

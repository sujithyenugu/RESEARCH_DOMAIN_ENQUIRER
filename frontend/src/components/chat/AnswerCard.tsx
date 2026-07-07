import React, { useState } from 'react';
import type { QueryResponse } from '../../types';
import ConfidenceBadge from './ConfidenceBadge';
import VerificationPanel from './VerificationPanel';
import GraphContext from './GraphContext';
import { MOCK_GRAPH_DATA } from '../../mock/data';

interface AnswerCardProps {
  response: QueryResponse;
  streamingText?: string;
  isStreaming?: boolean;
}

const AnswerCard: React.FC<AnswerCardProps> = ({ response, streamingText, isStreaming }) => {
  const [showVerification, setShowVerification] = useState(false);
  const [showGraph, setShowGraph] = useState(false);
  const [copied, setCopied] = useState(false);

  const displayText = isStreaming ? streamingText ?? '' : response.answer;

  const handleCopy = async () => {
    await navigator.clipboard.writeText(response.answer);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const formatText = (text: string) => {
    // Simple markdown-like formatting: **bold**, [paperId] highlights
    return text
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\[([\d.]+)\]/g, '<span class="badge badge-paper font-mono" style="font-size:0.72rem;padding:1px 6px;">[$1]</span>');
  };

  return (
    <div className="glass-card-accent anim-fade-in" style={{ padding: '20px' }}>
      {/* Header row */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '12px', gap: '12px', flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontSize: '0.65rem', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--color-text-muted)', marginBottom: '6px' }}>
            Answer
          </div>
          <ConfidenceBadge
            confidence={response.confidence}
            level={response.confidenceLevel}
            action={response.action}
          />
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
          <span className="badge badge-paper" style={{ fontSize: '0.72rem' }}>
            🔗 {response.citations.length} papers cited
          </span>
          <span style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)' }}>
            {response.latencyMs}ms
          </span>
        </div>
      </div>

      <div className="divider" />

      {/* Answer text */}
      <div
        className={isStreaming ? 'cursor-blink' : ''}
        style={{
          fontSize: '0.9375rem',
          lineHeight: 1.75,
          color: 'var(--color-text-primary)',
          marginBottom: '16px',
        }}
        dangerouslySetInnerHTML={{ __html: formatText(displayText) }}
      />

      {/* Action row */}
      {!isStreaming && (
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <button
            className="btn btn-secondary"
            onClick={() => setShowVerification(v => !v)}
            aria-expanded={showVerification}
            aria-controls="verification-panel"
          >
            {showVerification ? '▲ Hide Verification' : '▼ Show Verification'}
          </button>
          <button className="btn btn-secondary" onClick={handleCopy} aria-label="Copy answer">
            {copied ? '✓ Copied!' : '📋 Copy Answer'}
          </button>
          <button
            className="btn btn-secondary"
            onClick={() => setShowGraph(v => !v)}
            aria-expanded={showGraph}
          >
            {showGraph ? '▲ Hide Graph' : '🕸️ Graph Context'}
          </button>
        </div>
      )}

      {/* Verification panel */}
      {showVerification && !isStreaming && (
        <div id="verification-panel">
          <VerificationPanel report={response.verification} />
        </div>
      )}

      {/* Mini graph */}
      {showGraph && !isStreaming && (
        <div className="glass-card anim-slide-up" style={{ padding: '14px', marginTop: '12px' }}>
          <div className="section-label" style={{ marginBottom: '8px' }}>Knowledge Graph Context</div>
          <GraphContext data={MOCK_GRAPH_DATA} height={200} />
        </div>
      )}
    </div>
  );
};

export default AnswerCard;

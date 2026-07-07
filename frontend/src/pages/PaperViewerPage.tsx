import React, { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { getPaper, getPaperChunks } from '../services/api';
import type { PaperEntity, NodeType } from '../types';
import GraphContext from '../components/chat/GraphContext';
import { MOCK_GRAPH_DATA } from '../mock/data';
import './PaperViewerPage.css';

const TYPE_COLORS: Record<NodeType, string> = {
  Paper: '#3b82f6', Model: '#8b5cf6', Method: '#f59e0b',
  Dataset: '#ef4444', Concept: '#8b5cf6', Benchmark: '#6b7280',
};
const TYPE_EMOJIS: Record<NodeType, string> = {
  Paper: '📄', Model: '🤖', Method: '🔧', Dataset: '🗄️', Concept: '💡', Benchmark: '🏆',
};

const StarRating: React.FC<{ score: number }> = ({ score }) => {
  const stars = Math.round(score * 5);
  return (
    <span className="stars">
      {Array.from({ length: 5 }, (_, i) => (
        <span key={i} className={i < stars ? 'star-filled' : 'star-empty'}>★</span>
      ))}
    </span>
  );
};

const EntityBadge: React.FC<{ entity: PaperEntity }> = ({ entity }) => {
  const color = TYPE_COLORS[entity.type] ?? '#6b7280';
  const emoji = TYPE_EMOJIS[entity.type] ?? '●';
  return (
    <span
      className="badge"
      style={{
        background: `${color}15`,
        color,
        border: `1px solid ${color}30`,
        fontSize: '0.72rem',
        cursor: 'default',
      }}
      title={entity.role ?? entity.type}
    >
      {emoji} {entity.name}
    </span>
  );
};

const PaperViewerPage: React.FC = () => {
  const { paperId = '2106.09685' } = useParams<{ paperId: string }>();
  const navigate = useNavigate();
  const [currentPage, setCurrentPage] = useState(1);
  const totalPages = 20;

  const { data: paper, isLoading: paperLoading } = useQuery({
    queryKey: ['paper', paperId],
    queryFn: () => getPaper(paperId),
  });

  const { data: chunks, isLoading: chunksLoading } = useQuery({
    queryKey: ['paper-chunks', paperId],
    queryFn: () => getPaperChunks(paperId),
  });

  if (paperLoading) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh' }}>
        <div style={{ textAlign: 'center' }}>
          <div className="anim-spin" style={{ fontSize: '2rem', display: 'block', marginBottom: '12px' }}>⟳</div>
          <p style={{ color: 'var(--color-text-muted)' }}>Loading paper…</p>
        </div>
      </div>
    );
  }

  if (!paper) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh' }}>
        <div style={{ textAlign: 'center' }}>
          <p style={{ color: 'var(--color-accent-red)' }}>Paper not found: {paperId}</p>
          <button className="btn btn-secondary" onClick={() => navigate('/')} style={{ marginTop: '12px' }}>
            ← Back to Chat
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="paper-viewer-page">
      {/* Header */}
      <div className="paper-header">
        <button
          className="btn btn-secondary"
          onClick={() => navigate(-1)}
          aria-label="Go back"
          style={{ padding: '6px 14px', fontSize: '0.85rem' }}
        >
          ← Back
        </button>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 className="paper-title truncate">{paper.title}</h1>
        </div>
        <a
          href={`https://arxiv.org/abs/${paper.arxivId}`}
          target="_blank"
          rel="noopener noreferrer"
          className="btn btn-primary"
          style={{ fontSize: '0.85rem', flexShrink: 0 }}
        >
          arXiv ↗
        </a>
      </div>

      <div className="paper-body">
        {/* Left: Metadata + Entities + Citation graph */}
        <aside className="paper-left scroll-panel">
          {/* Metadata card */}
          <div className="glass-card anim-fade-in" style={{ padding: '18px', marginBottom: '14px' }}>
            <div className="section-label" style={{ marginBottom: '12px' }}>Metadata</div>

            <div style={{ marginBottom: '12px' }}>
              <div style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)', marginBottom: '3px' }}>Authors</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                {paper.authors.map((author, i) => (
                  <span key={i} style={{ fontSize: '0.82rem', color: 'var(--color-text-secondary)' }}>
                    {author}
                  </span>
                ))}
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', marginBottom: '12px' }}>
              <div>
                <div style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)', marginBottom: '2px' }}>Published</div>
                <div style={{ fontSize: '0.85rem', color: 'var(--color-text-primary)', fontWeight: 500 }}>
                  {new Date(paper.publishedDate).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })}
                </div>
              </div>
              <div>
                <div style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)', marginBottom: '2px' }}>arXiv ID</div>
                <div className="font-mono" style={{ fontSize: '0.85rem', color: 'var(--color-accent-blue)' }}>
                  {paper.arxivId}
                </div>
              </div>
              {paper.citationCount != null && (
                <div>
                  <div style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)', marginBottom: '2px' }}>Citations</div>
                  <div style={{ fontSize: '0.85rem', color: 'var(--color-accent-green)', fontWeight: 700 }}>
                    {paper.citationCount.toLocaleString()}
                  </div>
                </div>
              )}
              <div>
                <div style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)', marginBottom: '2px' }}>Chunks</div>
                <div style={{ fontSize: '0.85rem', color: 'var(--color-accent-purple)' }}>{paper.totalChunks}</div>
              </div>
            </div>

            <div style={{ marginBottom: '12px' }}>
              <div style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)', marginBottom: '5px' }}>Categories</div>
              <div style={{ display: 'flex', gap: '5px', flexWrap: 'wrap' }}>
                {paper.categories.map(cat => (
                  <span key={cat} className="chip" style={{ fontSize: '0.72rem' }}>{cat}</span>
                ))}
              </div>
            </div>

            <div>
              <div style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)', marginBottom: '5px' }}>Abstract</div>
              <p style={{ fontSize: '0.8rem', color: 'var(--color-text-secondary)', lineHeight: 1.65 }}>
                {paper.abstract.slice(0, 400)}{paper.abstract.length > 400 ? '…' : ''}
              </p>
            </div>
          </div>

          {/* Entities */}
          <div className="glass-card anim-fade-in" style={{ padding: '16px', marginBottom: '14px', animationDelay: '80ms' }}>
            <div className="section-label" style={{ marginBottom: '10px' }}>
              Entities in Paper ({paper.entities.length})
            </div>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
              {paper.entities.map(entity => (
                <EntityBadge key={entity.id} entity={entity} />
              ))}
            </div>
          </div>

          {/* Citation graph */}
          <div className="glass-card anim-fade-in" style={{ padding: '16px', animationDelay: '160ms' }}>
            <div className="section-label" style={{ marginBottom: '10px' }}>Citation Graph</div>
            <GraphContext data={MOCK_GRAPH_DATA} height={180} />
          </div>
        </aside>

        {/* Right: PDF viewer + chunks */}
        <div className="paper-right scroll-panel">
          {/* PDF viewer placeholder */}
          <div className="glass-card anim-fade-in" style={{ padding: '18px', marginBottom: '16px' }}>
            <div className="section-label" style={{ marginBottom: '12px' }}>PDF Viewer</div>

            {/* Mock PDF frame */}
            <div className="pdf-frame">
              <div className="pdf-content">
                <div style={{ textAlign: 'center', marginBottom: '24px' }}>
                  <h3 style={{ fontSize: '0.95rem', color: 'var(--color-text-primary)', marginBottom: '8px' }}>
                    {paper.title}
                  </h3>
                  <p style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>
                    {paper.authors.slice(0, 4).join(', ')}
                    {paper.authors.length > 4 ? ', et al.' : ''}
                  </p>
                </div>

                <div className="pdf-abstract-block">
                  <div style={{ fontSize: '0.7rem', fontWeight: 700, color: 'var(--color-text-muted)', textAlign: 'center', marginBottom: '8px' }}>
                    ABSTRACT
                  </div>
                  <p style={{ fontSize: '0.8rem', color: 'var(--color-text-secondary)', lineHeight: 1.7, textAlign: 'justify' }}>
                    {paper.abstract.slice(0, 600)}…
                  </p>
                </div>

                {/* Highlighted chunk indicator */}
                <div style={{
                  marginTop: '16px',
                  padding: '10px 12px',
                  background: 'rgba(59, 130, 246, 0.12)',
                  border: '2px solid rgba(59, 130, 246, 0.4)',
                  borderRadius: 'var(--radius-sm)',
                  fontSize: '0.78rem',
                  color: 'var(--color-text-primary)',
                  lineHeight: 1.6,
                }}>
                  <span style={{ fontSize: '0.65rem', color: 'var(--color-accent-blue)', fontWeight: 700, display: 'block', marginBottom: '4px' }}>
                    🔵 RETRIEVED CHUNK — ABSTRACT
                  </span>
                  {chunks?.[0]?.text.slice(0, 200)}…
                </div>

                <div style={{ marginTop: '16px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
                  {[1, 2, 3].map(i => (
                    <div key={i} style={{ height: '10px', borderRadius: '3px' }} className="skeleton" />
                  ))}
                  <div style={{ height: '10px', width: '70%', borderRadius: '3px' }} className="skeleton" />
                </div>
              </div>

              {/* Page info overlay */}
              <div style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)', marginTop: '8px' }}>
                Page {currentPage} of {totalPages} (mock layout)
              </div>
            </div>

            {/* Page navigation */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '12px', marginTop: '12px' }}>
              <button
                className="btn btn-secondary"
                onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
                disabled={currentPage === 1}
                aria-label="Previous page"
                style={{ padding: '6px 14px' }}
              >
                ◄
              </button>
              <span style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)', minWidth: '80px', textAlign: 'center' }}>
                {currentPage} / {totalPages}
              </span>
              <button
                className="btn btn-secondary"
                onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
                disabled={currentPage === totalPages}
                aria-label="Next page"
                style={{ padding: '6px 14px' }}
              >
                ►
              </button>
            </div>
          </div>

          {/* Retrieved chunks */}
          <div className="glass-card anim-fade-in" style={{ padding: '18px', animationDelay: '100ms' }}>
            <div className="section-label" style={{ marginBottom: '12px' }}>
              Retrieved Chunks from This Paper ({chunks?.length ?? 0})
            </div>

            {chunksLoading ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {[1, 2, 3].map(i => <div key={i} className="skeleton" style={{ height: '70px', borderRadius: 'var(--radius-md)' }} />)}
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                {chunks?.map((chunk, idx) => (
                  <div
                    key={chunk.chunkId}
                    className="glass-card anim-fade-in"
                    style={{
                      padding: '12px 14px',
                      borderLeft: `3px solid rgba(59,130,246,${chunk.relevanceScore})`,
                      animationDelay: `${idx * 60}ms`,
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--color-accent-blue)' }}>
                          {chunk.section}
                        </span>
                        {chunk.pageNumber && (
                          <span style={{ fontSize: '0.68rem', color: 'var(--color-text-muted)' }}>p.{chunk.pageNumber}</span>
                        )}
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                        <StarRating score={chunk.relevanceScore} />
                        <span style={{ fontSize: '0.7rem', color: 'var(--color-text-muted)', minWidth: '32px' }}>
                          {(chunk.relevanceScore * 100).toFixed(0)}%
                        </span>
                      </div>
                    </div>
                    <p style={{
                      fontSize: '0.8rem',
                      color: 'var(--color-text-secondary)',
                      lineHeight: 1.6,
                      fontStyle: 'italic',
                    }}>
                      "{chunk.text}"
                    </p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default PaperViewerPage;

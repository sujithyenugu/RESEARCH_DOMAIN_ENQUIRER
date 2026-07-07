import React from 'react';
import type { EntityDetails, NodeType } from '../../types';
import { NODE_COLORS } from './KnowledgeGraph';
import { useNavigate } from 'react-router-dom';

interface EntitySidebarProps {
  entity: EntityDetails | null;
  onClose: () => void;
}

const TYPE_EMOJIS: Record<NodeType, string> = {
  Paper:     '📄',
  Model:     '🤖',
  Method:    '🔧',
  Dataset:   '🗄️',
  Concept:   '💡',
  Benchmark: '🏆',
};

const EntitySidebar: React.FC<EntitySidebarProps> = ({ entity, onClose }) => {
  const navigate = useNavigate();

  if (!entity) {
    return (
      <div className="entity-sidebar glass-card" style={{ padding: '24px', textAlign: 'center' }}>
        <div style={{ fontSize: '2rem', marginBottom: '12px', opacity: 0.3 }}>🕸️</div>
        <p style={{ fontSize: '0.85rem', color: 'var(--color-text-muted)' }}>
          Click a node to see entity details
        </p>
      </div>
    );
  }

  const color = NODE_COLORS[entity.type];
  const emoji = TYPE_EMOJIS[entity.type];

  return (
    <div className="entity-sidebar glass-card anim-slide-right" style={{ padding: '0', overflow: 'hidden' }}>
      {/* Header */}
      <div style={{
        padding: '16px',
        borderBottom: '1px solid var(--color-border)',
        background: `linear-gradient(135deg, ${color}15, transparent)`,
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <span
              className="badge"
              style={{ background: `${color}20`, color, border: `1px solid ${color}40`, marginBottom: '6px' }}
            >
              {emoji} {entity.type}
            </span>
            <h3 style={{ fontSize: '0.95rem', fontWeight: 700, color: 'var(--color-text-primary)', lineHeight: 1.3 }}>
              {entity.name}
            </h3>
            {entity.year && (
              <p style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)', marginTop: '3px' }}>
                Introduced {entity.year}
              </p>
            )}
          </div>
          <button
            className="btn btn-ghost"
            onClick={onClose}
            aria-label="Close entity panel"
            style={{ padding: '4px', flexShrink: 0 }}
          >
            ✕
          </button>
        </div>
      </div>

      <div style={{ padding: '14px', overflowY: 'auto', maxHeight: 'calc(100% - 120px)' }}>
        {/* Description */}
        {entity.description && (
          <p style={{ fontSize: '0.8rem', color: 'var(--color-text-secondary)', marginBottom: '14px', lineHeight: 1.6 }}>
            {entity.description}
          </p>
        )}

        {/* Stats */}
        {entity.paperCount != null && (
          <div className="glass-card" style={{ padding: '10px 12px', marginBottom: '14px', display: 'flex', justifyContent: 'space-between' }}>
            <span style={{ fontSize: '0.8rem', color: 'var(--color-text-muted)' }}>Papers using this</span>
            <span style={{ fontSize: '0.9rem', fontWeight: 700, color }}>
              {entity.paperCount.toLocaleString()}
            </span>
          </div>
        )}

        {/* Properties */}
        {entity.properties && Object.keys(entity.properties).length > 0 && (
          <div style={{ marginBottom: '14px' }}>
            <div className="section-label" style={{ marginBottom: '8px' }}>Properties</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
              {Object.entries(entity.properties).map(([k, v]) => (
                <div
                  key={k}
                  style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.78rem', gap: '8px' }}
                >
                  <span style={{ color: 'var(--color-text-muted)', flexShrink: 0 }}>
                    {k.replace(/([A-Z])/g, ' $1').trim()}
                  </span>
                  <span style={{ color: 'var(--color-text-primary)', textAlign: 'right', fontFamily: 'var(--font-mono)' }}>
                    {String(v)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Related Entities */}
        {entity.relatedEntities.length > 0 && (
          <div>
            <div className="section-label" style={{ marginBottom: '8px' }}>Related Entities</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
              {entity.relatedEntities.map(rel => {
                const relColor = NODE_COLORS[rel.type];
                return (
                  <button
                    key={rel.id}
                    onClick={() => rel.type === 'Paper' && navigate(`/papers/${rel.id}`)}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '8px',
                      padding: '6px 10px',
                      background: 'rgba(255,255,255,0.03)',
                      border: '1px solid var(--color-border)',
                      borderRadius: 'var(--radius-sm)',
                      cursor: rel.type === 'Paper' ? 'pointer' : 'default',
                      textAlign: 'left',
                      transition: 'all var(--transition-fast)',
                      width: '100%',
                    }}
                    onMouseEnter={e => {
                      (e.currentTarget as HTMLButtonElement).style.background = 'rgba(255,255,255,0.06)';
                    }}
                    onMouseLeave={e => {
                      (e.currentTarget as HTMLButtonElement).style.background = 'rgba(255,255,255,0.03)';
                    }}
                  >
                    <span style={{
                      width: 8, height: 8, borderRadius: '50%',
                      background: relColor, flexShrink: 0,
                    }} />
                    <span style={{ flex: 1, fontSize: '0.78rem', color: 'var(--color-text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {rel.label}
                    </span>
                    <span style={{ fontSize: '0.65rem', color: 'var(--color-text-muted)', fontFamily: 'var(--font-mono)', flexShrink: 0 }}>
                      {rel.relation}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default EntitySidebar;

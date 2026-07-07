import React, { useState } from 'react';
import type { NodeType } from '../../types';
import { NODE_COLORS } from './KnowledgeGraph';
import { useGraphStore } from '../../store';

interface GraphControlsProps {
  onZoomIn: () => void;
  onZoomOut: () => void;
  onReset: () => void;
  onSearch: (term: string) => void;
  nodeCount: number;
  edgeCount: number;
}

const NODE_TYPES: NodeType[] = ['Paper', 'Model', 'Method', 'Dataset', 'Concept', 'Benchmark'];
const NODE_EMOJIS: Record<NodeType, string> = {
  Paper: '📄', Model: '🤖', Method: '🔧', Dataset: '🗄️', Concept: '💡', Benchmark: '🏆',
};

const GraphControls: React.FC<GraphControlsProps> = ({
  onZoomIn, onZoomOut, onReset, onSearch, nodeCount, edgeCount,
}) => {
  const [searchInput, setSearchInput] = useState('');
  const { typeFilters, toggleTypeFilter, hopDepth, setHopDepth, yearRange, setYearRange } = useGraphStore();

  const handleSearch = (val: string) => {
    setSearchInput(val);
    onSearch(val);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
      {/* Search */}
      <div style={{ position: 'relative' }}>
        <span style={{ position: 'absolute', left: '12px', top: '50%', transform: 'translateY(-50%)', fontSize: '0.9rem', pointerEvents: 'none', zIndex: 1 }}>
          🔍
        </span>
        <input
          className="input input-search"
          type="text"
          placeholder="Search entity…"
          value={searchInput}
          onChange={e => handleSearch(e.target.value)}
          aria-label="Search graph entity"
          style={{ paddingLeft: '38px' }}
        />
      </div>

      {/* Zoom controls */}
      <div className="glass-card" style={{ padding: '10px', display: 'flex', gap: '6px', justifyContent: 'center' }}>
        <button className="btn btn-secondary" onClick={onZoomIn} title="Zoom in" aria-label="Zoom in" style={{ flex: 1, padding: '6px' }}>+</button>
        <button className="btn btn-secondary" onClick={onZoomOut} title="Zoom out" aria-label="Zoom out" style={{ flex: 1, padding: '6px' }}>−</button>
        <button className="btn btn-secondary" onClick={onReset} title="Reset zoom" aria-label="Reset zoom" style={{ flex: 1, padding: '6px', fontSize: '0.8rem' }}>⟲</button>
      </div>

      {/* Graph stats */}
      <div className="glass-card" style={{ padding: '10px 12px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
          <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>Nodes</span>
          <span style={{ fontSize: '0.8rem', fontWeight: 700, color: 'var(--color-accent-blue)' }}>{nodeCount}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>Edges</span>
          <span style={{ fontSize: '0.8rem', fontWeight: 700, color: 'var(--color-accent-cyan)' }}>{edgeCount}</span>
        </div>
      </div>

      {/* Node type filters */}
      <div className="glass-card" style={{ padding: '12px' }}>
        <div className="section-label" style={{ marginBottom: '10px' }}>Vertex Types</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          {NODE_TYPES.map(type => {
            const color = NODE_COLORS[type];
            const active = typeFilters[type] !== false;
            return (
              <label
                key={type}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '8px',
                  cursor: 'pointer',
                  padding: '5px 8px',
                  borderRadius: 'var(--radius-sm)',
                  background: active ? `${color}12` : 'transparent',
                  border: `1px solid ${active ? color + '30' : 'transparent'}`,
                  transition: 'all var(--transition-fast)',
                }}
              >
                <span style={{ width: 10, height: 10, borderRadius: '50%', background: color, flexShrink: 0, opacity: active ? 1 : 0.3 }} />
                <span style={{ fontSize: '0.8rem', color: active ? 'var(--color-text-primary)' : 'var(--color-text-muted)', flex: 1 }}>
                  {NODE_EMOJIS[type]} {type}
                </span>
                <input
                  type="checkbox"
                  checked={active}
                  onChange={() => toggleTypeFilter(type)}
                  aria-label={`Toggle ${type} nodes`}
                  style={{ display: 'none' }}
                />
                <span style={{
                  width: 16, height: 16, borderRadius: '3px', display: 'flex', alignItems: 'center', justifyContent: 'center',
                  background: active ? color : 'var(--color-bg-tertiary)',
                  border: `1px solid ${active ? color : 'var(--color-border)'}`,
                  fontSize: '0.6rem', color: '#fff', flexShrink: 0,
                  transition: 'all var(--transition-fast)',
                }}>
                  {active ? '✓' : ''}
                </span>
              </label>
            );
          })}
        </div>
      </div>

      {/* Hop depth */}
      <div className="glass-card" style={{ padding: '12px' }}>
        <div className="section-label" style={{ marginBottom: '8px' }}>
          Hop Depth: <strong style={{ color: 'var(--color-accent-blue)' }}>{hopDepth}</strong>
        </div>
        <input
          type="range" min={1} max={4} step={1} value={hopDepth}
          onChange={e => setHopDepth(parseInt(e.target.value, 10))}
          style={{ width: '100%' }}
          aria-label={`Hop depth: ${hopDepth}`}
        />
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.7rem', color: 'var(--color-text-muted)', marginTop: '4px' }}>
          <span>1</span><span>2</span><span>3</span><span>4</span>
        </div>
      </div>

      {/* Year range */}
      <div className="glass-card" style={{ padding: '12px' }}>
        <div className="section-label" style={{ marginBottom: '8px' }}>
          Year Range: <strong style={{ color: 'var(--color-accent-blue)' }}>{yearRange[0]} – {yearRange[1]}</strong>
        </div>
        <input
          type="range" min={2015} max={2025} step={1} value={yearRange[0]}
          onChange={e => setYearRange([parseInt(e.target.value, 10), yearRange[1]])}
          style={{ width: '100%' }}
          aria-label={`Year from: ${yearRange[0]}`}
        />
        <input
          type="range" min={2015} max={2025} step={1} value={yearRange[1]}
          onChange={e => setYearRange([yearRange[0], parseInt(e.target.value, 10)])}
          style={{ width: '100%', marginTop: '6px' }}
          aria-label={`Year to: ${yearRange[1]}`}
        />
      </div>

      {/* Legend */}
      <div className="glass-card" style={{ padding: '12px' }}>
        <div className="section-label" style={{ marginBottom: '8px' }}>Controls</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          {[
            ['Scroll', 'Zoom in/out'],
            ['Drag', 'Pan graph'],
            ['Click node', 'View details'],
            ['Dbl-click', 'Expand neighborhood'],
            ['Hover edge', 'See relation'],
          ].map(([key, val]) => (
            <div key={key} style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.72rem' }}>
              <span style={{ color: 'var(--color-accent-cyan)' }}>{key}</span>
              <span style={{ color: 'var(--color-text-muted)' }}>{val}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

export default GraphControls;

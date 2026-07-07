import React, { useRef, useState, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import KnowledgeGraph, { type KnowledgeGraphHandle } from '../components/graph/KnowledgeGraph';
import GraphControls from '../components/graph/GraphControls';
import EntitySidebar from '../components/graph/EntitySidebar';
import { getGraphEntity, getEntityDetails } from '../services/api';
import { useGraphStore } from '../store';
import type { GraphNode, EntityDetails } from '../types';
import './GraphViewerPage.css';

const GraphViewerPage: React.FC = () => {
  const graphRef = useRef<KnowledgeGraphHandle>(null);
  const { typeFilters, yearRange, setSelectedNode } = useGraphStore();
  const [selectedEntity, setSelectedEntity] = useState<EntityDetails | null>(null);
  const [highlightNodeId, setHighlightNodeId] = useState<string | null>(null);

  const { data: graphData, isLoading } = useQuery({
    queryKey: ['graph', 'main'],
    queryFn: () => getGraphEntity('LoRA', 2),
    staleTime: 5 * 60 * 1000,
  });

  const handleNodeClick = useCallback(async (node: GraphNode) => {
    setSelectedNode(node.id);
    setHighlightNodeId(node.id);
    try {
      const details = await getEntityDetails(node.id);
      setSelectedEntity(details);
    } catch {
      setSelectedEntity({
        id: node.id,
        name: node.label,
        type: node.type,
        year: node.year,
        relatedEntities: [],
      });
    }
  }, [setSelectedNode]);

  const handleNodeDblClick = useCallback((node: GraphNode) => {
    graphRef.current?.centerNode(node.id);
  }, []);

  const handleSearch = useCallback((term: string) => {
    if (!term || !graphData) return;
    const found = graphData.nodes.find(n =>
      n.label.toLowerCase().includes(term.toLowerCase())
    );
    if (found) {
      setHighlightNodeId(found.id);
      graphRef.current?.centerNode(found.id);
    }
  }, [graphData]);

  const filteredNodes = graphData?.nodes.filter(n =>
    typeFilters[n.type] !== false &&
    (n.year == null || (n.year >= yearRange[0] && n.year <= yearRange[1]))
  ) ?? [];

  return (
    <div className="graph-viewer-page">
      {/* Header */}
      <div className="graph-header glass-card" style={{ borderRadius: 0, borderBottom: '1px solid var(--color-border)', borderLeft: 'none', borderRight: 'none', borderTop: 'none' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <span style={{ fontSize: '1.25rem' }}>🕸️</span>
          <div>
            <h1 style={{ fontSize: '1.1rem', fontWeight: 700, margin: 0 }}>Knowledge Graph Explorer</h1>
            <p style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)', margin: 0 }}>
              {filteredNodes.length} entities · Force-directed visualization
            </p>
          </div>
        </div>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <span className="badge badge-pass" style={{ animation: 'pulse 2s infinite' }}>
            ● Live
          </span>
          {isLoading && (
            <span style={{ fontSize: '0.8rem', color: 'var(--color-text-muted)' }}>Loading graph…</span>
          )}
        </div>
      </div>

      <div className="graph-body">
        {/* Left controls */}
        <aside className="graph-left-panel scroll-panel">
          {graphData && (
            <GraphControls
              onZoomIn={() => graphRef.current?.zoomIn()}
              onZoomOut={() => graphRef.current?.zoomOut()}
              onReset={() => graphRef.current?.resetZoom()}
              onSearch={handleSearch}
              nodeCount={filteredNodes.length}
              edgeCount={graphData.edges.length}
            />
          )}
        </aside>

        {/* Main graph canvas */}
        <div className="graph-canvas">
          {isLoading ? (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', flexDirection: 'column', gap: '16px' }}>
              <div className="anim-spin" style={{ fontSize: '2rem' }}>⟳</div>
              <p style={{ color: 'var(--color-text-muted)' }}>Loading knowledge graph…</p>
            </div>
          ) : graphData ? (
            <KnowledgeGraph
              ref={graphRef}
              data={graphData}
              onNodeClick={handleNodeClick}
              onNodeDblClick={handleNodeDblClick}
              highlightNodeId={highlightNodeId}
              typeFilters={typeFilters}
              yearRange={yearRange}
            />
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
              <p style={{ color: 'var(--color-text-muted)' }}>No graph data available</p>
            </div>
          )}

          {/* Legend overlay */}
          {!isLoading && (
            <div className="graph-legend glass-card">
              {[
                { type: 'Paper', color: '#3b82f6' },
                { type: 'Model', color: '#8b5cf6' },
                { type: 'Method', color: '#f59e0b' },
                { type: 'Dataset', color: '#ef4444' },
                { type: 'Concept', color: '#8b5cf6' },
                { type: 'Benchmark', color: '#6b7280' },
              ].filter(item => typeFilters[item.type] !== false).map(item => (
                <div key={item.type} style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: item.color, display: 'inline-block' }} />
                  <span style={{ fontSize: '0.7rem', color: 'var(--color-text-muted)' }}>{item.type}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Right entity sidebar */}
        <aside className="graph-right-panel scroll-panel">
          <EntitySidebar
            entity={selectedEntity}
            onClose={() => {
              setSelectedEntity(null);
              setSelectedNode(null);
              setHighlightNodeId(null);
            }}
          />
        </aside>
      </div>
    </div>
  );
};

export default GraphViewerPage;

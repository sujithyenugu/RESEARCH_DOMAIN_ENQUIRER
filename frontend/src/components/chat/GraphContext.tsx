import React, { useEffect, useRef, useCallback } from 'react';
import * as d3 from 'd3';
import type { GraphData, GraphNode, GraphEdge } from '../../types';
import { useNavigate } from 'react-router-dom';

const NODE_COLORS: Record<string, string> = {
  Paper:     '#3b82f6',
  Model:     '#8b5cf6',
  Method:    '#f59e0b',
  Dataset:   '#ef4444',
  Concept:   '#8b5cf6',
  Benchmark: '#6b7280',
};

interface GraphContextProps {
  data: GraphData;
  height?: number;
}

const GraphContext: React.FC<GraphContextProps> = ({ data, height = 220 }) => {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const navigate = useNavigate();

  const render = useCallback(() => {
    const el = svgRef.current;
    if (!el) return;

    const width = el.clientWidth || 400;
    d3.select(el).selectAll('*').remove();

    const svg = d3.select(el)
      .attr('width', width)
      .attr('height', height);

    // Gradient defs
    const defs = svg.append('defs');
    defs.append('marker')
      .attr('id', 'ctx-arrow')
      .attr('viewBox', '0 -4 8 8')
      .attr('refX', 14)
      .attr('refY', 0)
      .attr('markerWidth', 6)
      .attr('markerHeight', 6)
      .attr('orient', 'auto')
      .append('path')
      .attr('d', 'M0,-4L8,0L0,4')
      .attr('fill', 'rgba(255,255,255,0.3)');

    const nodes: GraphNode[] = data.nodes.slice(0, 15).map(n => ({ ...n }));
    const nodeIds = new Set(nodes.map(n => n.id));
    const links: GraphEdge[] = data.edges
      .filter(e => {
        const src = typeof e.source === 'string' ? e.source : e.source.id;
        const tgt = typeof e.target === 'string' ? e.target : e.target.id;
        return nodeIds.has(src) && nodeIds.has(tgt);
      })
      .slice(0, 20)
      .map(e => ({ ...e }));

    const sim = d3.forceSimulation(nodes as d3.SimulationNodeDatum[])
      .force('link', d3.forceLink(links).id((d: d3.SimulationNodeDatum) => (d as GraphNode).id).distance(60))
      .force('charge', d3.forceManyBody().strength(-80))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide(20));

    const linkEls = svg.append('g')
      .selectAll('line')
      .data(links)
      .join('line')
      .attr('stroke', 'rgba(255,255,255,0.12)')
      .attr('stroke-width', 1)
      .attr('marker-end', 'url(#ctx-arrow)');

    const nodeEls = svg.append('g')
      .selectAll('g')
      .data(nodes)
      .join('g')
      .style('cursor', 'pointer')
      .on('click', (_, d) => {
        if (d.type === 'Paper') navigate(`/papers/${d.id}`);
      });

    nodeEls.append('circle')
      .attr('r', 7)
      .attr('fill', d => NODE_COLORS[d.type] ?? '#6b7280')
      .attr('stroke', d => NODE_COLORS[d.type] ?? '#6b7280')
      .attr('stroke-width', 2)
      .attr('stroke-opacity', 0.4)
      .attr('fill-opacity', 0.85);

    nodeEls.append('text')
      .text(d => d.label.length > 14 ? d.label.slice(0, 13) + '…' : d.label)
      .attr('font-size', 9)
      .attr('fill', 'rgba(255,255,255,0.65)')
      .attr('text-anchor', 'middle')
      .attr('dy', 18)
      .style('pointer-events', 'none');

    sim.on('tick', () => {
      linkEls
        .attr('x1', d => (d.source as GraphNode).x ?? 0)
        .attr('y1', d => (d.source as GraphNode).y ?? 0)
        .attr('x2', d => (d.target as GraphNode).x ?? 0)
        .attr('y2', d => (d.target as GraphNode).y ?? 0);

      nodeEls.attr('transform', d => `translate(${(d as GraphNode).x ?? 0},${(d as GraphNode).y ?? 0})`);
    });

    return () => { sim.stop(); };
  }, [data, height, navigate]);

  useEffect(() => {
    const cleanup = render();
    return cleanup;
  }, [render]);

  return (
    <div style={{ width: '100%', overflow: 'hidden', borderRadius: 'var(--radius-md)' }}>
      <svg ref={svgRef} style={{ width: '100%', height, display: 'block' }} aria-label="Knowledge graph context" />
    </div>
  );
};

export default GraphContext;

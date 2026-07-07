import { useEffect, useRef, useCallback, forwardRef, useImperativeHandle } from 'react';
import * as d3 from 'd3';
import type { GraphData, GraphNode, GraphEdge, NodeType } from '../../types';

export const NODE_COLORS: Record<NodeType, string> = {
  Paper:     '#3b82f6',
  Model:     '#8b5cf6',
  Method:    '#f59e0b',
  Dataset:   '#ef4444',
  Concept:   '#8b5cf6',
  Benchmark: '#6b7280',
};

const NODE_RADIUS: Record<NodeType, number> = {
  Paper:     10,
  Model:     9,
  Method:    9,
  Dataset:   8,
  Concept:   8,
  Benchmark: 7,
};

interface KnowledgeGraphProps {
  data: GraphData;
  onNodeClick?: (node: GraphNode) => void;
  onNodeDblClick?: (node: GraphNode) => void;
  highlightNodeId?: string | null;
  typeFilters: Record<string, boolean>;
  yearRange: [number, number];
}

export interface KnowledgeGraphHandle {
  centerNode: (nodeId: string) => void;
  zoomIn: () => void;
  zoomOut: () => void;
  resetZoom: () => void;
}

const KnowledgeGraph = forwardRef<KnowledgeGraphHandle, KnowledgeGraphProps>(
  ({ data, onNodeClick, onNodeDblClick, highlightNodeId, typeFilters, yearRange }, ref) => {
    const svgRef = useRef<SVGSVGElement | null>(null);
    const simulationRef = useRef<d3.Simulation<GraphNode, GraphEdge> | null>(null);
    const zoomRef = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null);
    const nodeMapRef = useRef<Map<string, GraphNode>>(new Map());
    const containerRef = useRef<d3.Selection<SVGGElement, unknown, null, undefined> | null>(null);

    const filteredNodes = data.nodes.filter(n =>
      typeFilters[n.type] !== false &&
      (n.year == null || (n.year >= yearRange[0] && n.year <= yearRange[1]))
    );
    const filteredNodeIds = new Set(filteredNodes.map(n => n.id));
    const filteredEdges = data.edges.filter(e => {
      const src = typeof e.source === 'string' ? e.source : (e.source as GraphNode).id;
      const tgt = typeof e.target === 'string' ? e.target : (e.target as GraphNode).id;
      return filteredNodeIds.has(src) && filteredNodeIds.has(tgt);
    });

    const render = useCallback(() => {
      const el = svgRef.current;
      if (!el) return;

      const width = el.clientWidth || 900;
      const height = el.clientHeight || 600;

      d3.select(el).selectAll('*').remove();

      const svg = d3.select(el);

      // Defs: arrow marker
      const defs = svg.append('defs');
      defs.append('marker')
        .attr('id', 'kg-arrow')
        .attr('viewBox', '0 -4 8 8')
        .attr('refX', 16)
        .attr('refY', 0)
        .attr('markerWidth', 6)
        .attr('markerHeight', 6)
        .attr('orient', 'auto')
        .append('path')
        .attr('d', 'M0,-4L8,0L0,4')
        .attr('fill', 'rgba(255,255,255,0.25)');

      // Add radial gradient for background
      const bgGrad = defs.append('radialGradient').attr('id', 'bg-grad');
      bgGrad.append('stop').attr('offset', '0%').attr('stop-color', '#1a2035');
      bgGrad.append('stop').attr('offset', '100%').attr('stop-color', '#0a0e1a');
      svg.append('rect').attr('width', width).attr('height', height).attr('fill', 'url(#bg-grad)');

      // Zoom
      const zoom = d3.zoom<SVGSVGElement, unknown>()
        .scaleExtent([0.15, 4])
        .on('zoom', e => {
          g.attr('transform', e.transform);
        });
      zoomRef.current = zoom;
      svg.call(zoom);

      const g = svg.append('g').attr('class', 'kg-root');
      containerRef.current = g;

      const nodes: GraphNode[] = filteredNodes.map(n => ({ ...n }));
      const links: GraphEdge[] = filteredEdges.map(e => ({ ...e }));

      nodeMapRef.current = new Map(nodes.map(n => [n.id, n]));

      // Simulation — cast to generic Simulation<GraphNode, GraphEdge> after initialisation
      const sim = d3.forceSimulation<GraphNode>(nodes)
        .force('link', d3.forceLink<GraphNode, GraphEdge>(links).id(d => d.id).distance(90).strength(0.5))
        .force('charge', d3.forceManyBody<GraphNode>().strength(-220))
        .force('center', d3.forceCenter(width / 2, height / 2))
        .force('collision', d3.forceCollide<GraphNode>(18))
        .alpha(1)
        .alphaDecay(0.028);
      simulationRef.current = sim;

      // Links
      const linkGroup = g.append('g').attr('class', 'links');
      const linkEls = linkGroup.selectAll<SVGLineElement, GraphEdge>('line')
        .data(links)
        .join('line')
        .attr('stroke', 'rgba(255,255,255,0.1)')
        .attr('stroke-width', 1.2)
        .attr('marker-end', 'url(#kg-arrow)');

      // Edge labels (visible on hover via tooltip)
      const edgeLabelEls = linkGroup.selectAll<SVGTextElement, GraphEdge>('text.edge-label')
        .data(links)
        .join('text')
        .attr('class', 'edge-label')
        .attr('font-size', 9)
        .attr('fill', 'rgba(255,255,255,0)') // hidden by default
        .attr('text-anchor', 'middle')
        .text(d => d.type)
        .style('pointer-events', 'none')
        .style('user-select', 'none');

      // Show/hide edge labels on link hover
      linkEls
        .on('mouseenter', function(_, d) {
          d3.select(this).attr('stroke', 'rgba(59,130,246,0.5)').attr('stroke-width', 2);
          edgeLabelEls.filter(ld => ld.id === d.id).attr('fill', 'rgba(255,255,255,0.7)');
        })
        .on('mouseleave', function(_, d) {
          d3.select(this).attr('stroke', 'rgba(255,255,255,0.1)').attr('stroke-width', 1.2);
          edgeLabelEls.filter(ld => ld.id === d.id).attr('fill', 'rgba(255,255,255,0)');
        });

      // Nodes
      const nodeGroup = g.append('g').attr('class', 'nodes');
      const nodeEls = nodeGroup.selectAll<SVGGElement, GraphNode>('g.node')
        .data(nodes)
        .join('g')
        .attr('class', 'node')
        .style('cursor', 'pointer')
        .call(
          d3.drag<SVGGElement, GraphNode>()
            .on('start', (event, d) => {
              if (!event.active) sim.alphaTarget(0.3).restart();
              d.fx = d.x; d.fy = d.y;
            })
            .on('drag', (event, d) => {
              d.fx = event.x; d.fy = event.y;
            })
            .on('end', (event, d) => {
              if (!event.active) sim.alphaTarget(0);
              d.fx = null; d.fy = null;
            })
        )
        .on('click', (_, d) => onNodeClick?.(d))
        .on('dblclick', (_, d) => onNodeDblClick?.(d));

      // Outer glow ring
      nodeEls.append('circle')
        .attr('r', d => NODE_RADIUS[d.type] + 4)
        .attr('fill', 'none')
        .attr('stroke', d => NODE_COLORS[d.type])
        .attr('stroke-width', 1)
        .attr('stroke-opacity', 0.25);

      // Main circle
      nodeEls.append('circle')
        .attr('r', d => NODE_RADIUS[d.type])
        .attr('fill', d => NODE_COLORS[d.type])
        .attr('fill-opacity', 0.85)
        .attr('stroke', '#0a0e1a')
        .attr('stroke-width', 1.5);

      // Label
      nodeEls.append('text')
        .text(d => d.label.length > 18 ? d.label.slice(0, 17) + '…' : d.label)
        .attr('font-size', 10)
        .attr('fill', 'rgba(255,255,255,0.8)')
        .attr('text-anchor', 'middle')
        .attr('dy', d => NODE_RADIUS[d.type] + 14)
        .style('pointer-events', 'none')
        .style('user-select', 'none');

      // Hover interaction
      nodeEls
        .on('mouseenter', function() {
          d3.select(this).select('circle:last-of-type')
            .attr('stroke-width', 3)
            .attr('fill-opacity', 1);
          d3.select(this).select('circle:first-of-type')
            .attr('stroke-opacity', 0.6);
        })
        .on('mouseleave', function() {
          d3.select(this).select('circle:last-of-type')
            .attr('stroke-width', 1.5)
            .attr('fill-opacity', 0.85);
          d3.select(this).select('circle:first-of-type')
            .attr('stroke-opacity', 0.25);
        });

      // Tick
      sim.on('tick', () => {
        linkEls
          .attr('x1', d => (d.source as GraphNode).x ?? 0)
          .attr('y1', d => (d.source as GraphNode).y ?? 0)
          .attr('x2', d => (d.target as GraphNode).x ?? 0)
          .attr('y2', d => (d.target as GraphNode).y ?? 0);

        edgeLabelEls
          .attr('x', d => {
            const sx = (d.source as GraphNode).x ?? 0;
            const tx = (d.target as GraphNode).x ?? 0;
            return (sx + tx) / 2;
          })
          .attr('y', d => {
            const sy = (d.source as GraphNode).y ?? 0;
            const ty = (d.target as GraphNode).y ?? 0;
            return (sy + ty) / 2;
          });

        nodeEls.attr('transform', d => `translate(${(d as GraphNode).x ?? 0},${(d as GraphNode).y ?? 0})`);
      });

      return () => { sim.stop(); };
    }, [filteredNodes, filteredEdges, onNodeClick, onNodeDblClick]);

    // Highlight selected node
    useEffect(() => {
      if (!svgRef.current || !highlightNodeId) return;
      d3.select(svgRef.current)
        .selectAll<SVGGElement, GraphNode>('g.node')
        .select('circle:last-of-type')
        .attr('stroke', d => d.id === highlightNodeId ? '#fff' : '#0a0e1a')
        .attr('stroke-width', d => d.id === highlightNodeId ? 3 : 1.5);
    }, [highlightNodeId]);

    useEffect(() => {
      const cleanup = render();
      return cleanup;
    }, [render]);

    // Imperative handle for external controls
    useImperativeHandle(ref, () => ({
      centerNode: (nodeId: string) => {
        const node = nodeMapRef.current.get(nodeId);
        const svgEl = svgRef.current;
        const zoom = zoomRef.current;
        if (!node || !svgEl || !zoom || node.x == null || node.y == null) return;
        const width = svgEl.clientWidth;
        const height = svgEl.clientHeight;
        d3.select(svgEl).transition().duration(600).call(
          zoom.transform,
          d3.zoomIdentity.translate(width / 2 - node.x * 1.5, height / 2 - node.y * 1.5).scale(1.5)
        );
      },
      zoomIn: () => {
        if (svgRef.current && zoomRef.current)
          d3.select(svgRef.current).transition().duration(300).call(zoomRef.current.scaleBy, 1.4);
      },
      zoomOut: () => {
        if (svgRef.current && zoomRef.current)
          d3.select(svgRef.current).transition().duration(300).call(zoomRef.current.scaleBy, 0.7);
      },
      resetZoom: () => {
        if (svgRef.current && zoomRef.current)
          d3.select(svgRef.current).transition().duration(400).call(zoomRef.current.transform, d3.zoomIdentity);
      },
    }));

    return (
      <svg
        ref={svgRef}
        style={{ width: '100%', height: '100%', display: 'block', borderRadius: 'var(--radius-lg)' }}
        aria-label="Knowledge graph visualization"
      />
    );
  }
);

KnowledgeGraph.displayName = 'KnowledgeGraph';

export default KnowledgeGraph;

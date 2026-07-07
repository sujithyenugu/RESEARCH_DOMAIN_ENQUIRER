// ============================================================
// Zustand Global Store
// ============================================================

import { create } from 'zustand';
import type { ChatMessage, ChatSession, QueryFilters, QueryResponse } from '../types';

// --- Types --------------------------------------------------

interface ChatState {
  sessions: ChatSession[];
  activeSessionId: string | null;
  isStreaming: boolean;
  streamingText: string;
  pendingResponse: QueryResponse | null;
  filters: QueryFilters;

  // Actions
  createSession: () => void;
  setActiveSession: (id: string) => void;
  addUserMessage: (sessionId: string, content: string) => void;
  setStreaming: (val: boolean) => void;
  appendStreamToken: (token: string) => void;
  finalizeStream: (sessionId: string, response: QueryResponse) => void;
  resetStream: () => void;
  updateFilters: (filters: Partial<QueryFilters>) => void;
}

interface SidebarState {
  collapsed: boolean;
  toggleCollapsed: () => void;
}

interface GraphState {
  selectedNodeId: string | null;
  searchTerm: string;
  hopDepth: number;
  typeFilters: Record<string, boolean>;
  yearRange: [number, number];

  setSelectedNode: (id: string | null) => void;
  setSearchTerm: (term: string) => void;
  setHopDepth: (depth: number) => void;
  toggleTypeFilter: (type: string) => void;
  setYearRange: (range: [number, number]) => void;
}

// --- Chat Store ---------------------------------------------

const makeSession = (): ChatSession => ({
  id: `session-${Date.now()}`,
  title: 'New Chat',
  messages: [],
  createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
});

export const useChatStore = create<ChatState>((set) => ({
  sessions: [makeSession()],
  activeSessionId: null,
  isStreaming: false,
  streamingText: '',
  pendingResponse: null,
  filters: {
    topK: 10,
    dateFrom: '2018-01-01',
    dateTo: new Date().toISOString().slice(0, 10),
    categories: [],
  },

  createSession: () => {
    const session = makeSession();
    set(state => ({
      sessions: [session, ...state.sessions],
      activeSessionId: session.id,
    }));
  },

  setActiveSession: (id) => set({ activeSessionId: id }),

  addUserMessage: (sessionId, content) => {
    const msg: ChatMessage = {
      id: `msg-${Date.now()}`,
      role: 'user',
      content,
      timestamp: new Date().toISOString(),
    };
    set(state => ({
      sessions: state.sessions.map(s =>
        s.id === sessionId
          ? { ...s, messages: [...s.messages, msg], title: s.messages.length === 0 ? content.slice(0, 50) : s.title }
          : s
      ),
    }));
  },

  setStreaming: (val) => set({ isStreaming: val, streamingText: val ? '' : '' }),

  appendStreamToken: (token) =>
    set(state => ({ streamingText: state.streamingText + token })),

  finalizeStream: (sessionId, response) => {
    const msg: ChatMessage = {
      id: `msg-${Date.now()}`,
      role: 'assistant',
      content: response.answer,
      timestamp: new Date().toISOString(),
      response,
    };
    set(state => ({
      isStreaming: false,
      streamingText: '',
      pendingResponse: response,
      sessions: state.sessions.map(s =>
        s.id === sessionId
          ? { ...s, messages: [...s.messages, msg], updatedAt: new Date().toISOString() }
          : s
      ),
    }));
  },

  resetStream: () => set({ isStreaming: false, streamingText: '', pendingResponse: null }),

  updateFilters: (filters) =>
    set(state => ({ filters: { ...state.filters, ...filters } })),
}));

// --- Sidebar Store ------------------------------------------

export const useSidebarStore = create<SidebarState>((set) => ({
  collapsed: false,
  toggleCollapsed: () => set(state => ({ collapsed: !state.collapsed })),
}));

// --- Graph Store --------------------------------------------

export const useGraphStore = create<GraphState>((set) => ({
  selectedNodeId: null,
  searchTerm: '',
  hopDepth: 2,
  typeFilters: {
    Paper: true,
    Model: true,
    Method: true,
    Dataset: true,
    Concept: true,
    Benchmark: true,
  },
  yearRange: [2017, 2024],

  setSelectedNode: (id) => set({ selectedNodeId: id }),
  setSearchTerm: (term) => set({ searchTerm: term }),
  setHopDepth: (depth) => set({ hopDepth: depth }),
  toggleTypeFilter: (type) =>
    set(state => ({
      typeFilters: {
        ...state.typeFilters,
        [type]: !state.typeFilters[type],
      },
    })),
  setYearRange: (range) => set({ yearRange: range }),
}));

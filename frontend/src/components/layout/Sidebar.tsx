import React from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { useSidebarStore } from '../../store';
import './Sidebar.css';

interface NavItem {
  icon: string;
  label: string;
  path: string;
}

const NAV_ITEMS: NavItem[] = [
  { icon: '💬', label: 'Chat', path: '/' },
  { icon: '🕸️', label: 'Knowledge Graph', path: '/graph' },
  { icon: '📄', label: 'Papers', path: '/papers/2106.09685' },
  { icon: '📊', label: 'Evaluation', path: '/evaluation' },
];

const Sidebar: React.FC = () => {
  const { collapsed, toggleCollapsed } = useSidebarStore();
  const location = useLocation();

  return (
    <aside className={`sidebar ${collapsed ? 'sidebar--collapsed' : ''}`}>
      {/* Logo */}
      <div className="sidebar__logo">
        <span className="sidebar__logo-icon">🔬</span>
        {!collapsed && (
          <div className="sidebar__logo-text">
            <span className="sidebar__logo-title">Research</span>
            <span className="sidebar__logo-sub">Enquirer</span>
          </div>
        )}
      </div>

      <div className="sidebar__divider" />

      {/* Navigation */}
      <nav className="sidebar__nav" aria-label="Main navigation">
        {NAV_ITEMS.map(item => {
          const isActive =
            item.path === '/'
              ? location.pathname === '/'
              : location.pathname.startsWith(item.path);

          return (
            <NavLink
              key={item.path}
              to={item.path}
              className={`sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`}
              title={collapsed ? item.label : undefined}
              aria-label={item.label}
            >
              <span className="sidebar__nav-icon">{item.icon}</span>
              {!collapsed && (
                <span className="sidebar__nav-label">{item.label}</span>
              )}
              {!collapsed && isActive && (
                <span className="sidebar__nav-dot" aria-hidden="true" />
              )}
            </NavLink>
          );
        })}
      </nav>

      <div className="sidebar__spacer" />

      {/* Status indicator */}
      {!collapsed && (
        <div className="sidebar__status">
          <span className="sidebar__status-dot" aria-hidden="true" />
          <span className="sidebar__status-text">Mock API Active</span>
        </div>
      )}

      {/* Collapse toggle */}
      <button
        className="sidebar__toggle"
        onClick={toggleCollapsed}
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        title={collapsed ? 'Expand' : 'Collapse'}
      >
        <span className="sidebar__toggle-icon">
          {collapsed ? '›' : '‹'}
        </span>
      </button>
    </aside>
  );
};

export default Sidebar;

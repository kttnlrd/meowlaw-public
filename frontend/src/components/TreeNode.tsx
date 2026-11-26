
import React, { memo, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Part, Division, Subdivision, Section, Signpost, Tree, COLORS } from './common/types';

function TreeNode({ node, level, activeSection, onSelect, isMobile, expandedIds, act, getHref }: {
  node: Part | Division | Subdivision | Section | Signpost;
  level: number;
  activeSection: string;
  onSelect: (id: string) => void;
  isMobile: boolean;
  expandedIds: Set<string>;
  act?: string;
  /** Canonical href for a leaf section id, so rows render as real <Link>s
   *  (F-15: right-click / middle-click / ctrl-click work natively). Returns
   *  null/undefined for ids that aren't real navigation targets (e.g. a
   *  private-rulings month bucket that only expands the tree). */
  getHref?: (id: string) => string | null | undefined;
}) {
  const [expanded, setExpanded] = useState(expandedIds.has((node as any).id));
  const isSignpost = 'is_signpost' in node && (node as any).is_signpost;
  const isSection = !isSignpost && 'path' in node;
  const isPart = !isSignpost && 'divisions' in node;
  const isDivision = !isSignpost && 'subdivisions' in node && 'sections' in node && !isPart;
  const isSubdivision = !isSignpost && 'sections' in node && !('subdivisions' in node) && !isPart && !isSection;
  const hasChildren = !isSignpost && !isSection && (
    (isPart && (((node as Part).divisions || []).length > 0 || ((node as Part).sections || []).length > 0)) ||
    (isDivision && (((node as Division).subdivisions || []).length > 0 || ((node as Division).sections || []).length > 0)) ||
    (isSubdivision && ((node as Subdivision).sections || []).length > 0)
  );

  // Sync expanded state when expandedIds changes (for auto-collapse/expand)
  useEffect(() => {
    if (expandedIds.has((node as any).id)) {
      setExpanded(true);
    }
  }, [expandedIds, (node as any).id]);

  const toggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    setExpanded(!expanded);
  };

  const displayId = isSection ? (node as Section).id : (node as Part | Division | Subdivision | Signpost).id;
  const displayTitle = isSection
    ? (node as Section).title
    : (node as Part | Division | Subdivision | Signpost).title;

  // Heuristic: slug-like IDs (contain hyphens + letters) look ugly in tree; hide them
  // Also hide CCH chapter IDs (ch-01, topic-03) since the title already contains the label
  // Also hide case category-year IDs (tax-2025, asic-2024)
  // Also hide case category part IDs (tax, asic, other)
  // Also hide ruling division IDs (2024-lcg, 2024-pcg)
  const isSlugLike = /^(ch|topic)-\d+$/i.test(displayId) || (isSection && /[a-z].*-[a-z]/.test(displayId)) || /^(tax|asic|other)-\d{4}$/i.test(displayId) || /^(tax|asic|other)$/i.test(displayId) || /^\d{4}-[a-z]+$/i.test(displayId) || /^[a-z]+-\d{4}$/i.test(displayId);
  const showId = !isSlugLike && displayId !== displayTitle && act !== 'treaties' && act !== 'private-rulings';

  const indent = isMobile ? Math.min(level * 10, 40) : level * 14;

  if (isSignpost) {
    return (
      <div id={`tree-node-${displayId}`} style={{ marginLeft: indent }}>
        <div style={{
          padding: isMobile ? '8px 8px' : '6px 6px',
          borderTop: `1px solid ${COLORS.border}`,
          color: COLORS.textMuted,
          fontWeight: 600,
          fontSize: 10,
          fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
          textTransform: 'uppercase',
          minHeight: isMobile ? 32 : 28,
          display: 'flex',
          alignItems: 'center',
        }}>
          Part {displayId} — {displayTitle}
        </div>
      </div>
    );
  }

  // Leaf sections navigate to a real URL — render as <Link> so right-click /
  // middle-click / ctrl-click "open in new tab" work natively (F-15). Other
  // rows (parts/divisions/subdivisions, and the private-rulings month
  // buckets that only expand the tree) stay plain, non-navigating divs.
  const href = isSection ? getHref?.((node as Section).id) : null;
  const navigable = isSection && !!href;
  const rowStyle: React.CSSProperties = {
    padding: isMobile ? '4px 8px' : '2px 6px',
    cursor: 'pointer',
    borderRadius: 4,
    background: isSection && (node as Section).id === activeSection ? 'rgba(39,158,136,0.12)' : 'transparent',
    color: isSection ? COLORS.text : COLORS.textMuted,
    fontWeight: isSection ? (act === 'maps' ? 600 : 400) : 500,
    fontSize: isMobile ? 13 : 12,
    fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
    display: 'flex',
    alignItems: isMobile ? 'flex-start' : 'center',
    whiteSpace: isMobile ? 'normal' : 'nowrap',
    overflow: 'hidden',
    minHeight: isMobile ? 40 : 28,
    lineHeight: isMobile ? 1.35 : 1.2,
    flex: 1,
    minWidth: 0,
    textDecoration: 'none',
  };
  const rowOnClick = () => {
    if (isSection) onSelect((node as Section).id);
    // Private rulings: years expand into months; clicking a month loads its ruling list
    else if (act === 'private-rulings' && isSubdivision) onSelect((node as Subdivision).id);
    else setExpanded(!expanded);
  };
  const rowClick = (e: React.MouseEvent) => {
    // Modified/right clicks on a real <a href> must fall through to the
    // browser's own "open in new tab" behaviour, not our in-app selection.
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    rowOnClick();
  };

  const rowContent = (
    <>
      {hasChildren && (
        <span onClick={toggle} style={{
          width: 28, minHeight: isMobile ? 28 : 20, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          textAlign: 'center', fontSize: 10,
          color: COLORS.textMuted, flexShrink: 0,
          marginTop: isMobile ? 2 : 0,
        }}>
          {expanded ? '▼' : '▶'}
        </span>
      )}
      {!hasChildren && <span style={{ width: 28, display: 'inline-block', flexShrink: 0 }} />}
      {(isSection && (act === 'cases' || act === 'rulings' || act === 'tax-cases')) ? (
        <>
          {displayTitle && (
            <span style={{ marginLeft: 4, whiteSpace: isMobile ? 'normal' : 'nowrap', overflow: 'hidden', textOverflow: isMobile ? 'clip' : 'ellipsis', fontWeight: 400 }}>
              {displayTitle}
            </span>
          )}
          {showId && (
            <span style={{ marginLeft: 6, whiteSpace: isMobile ? 'normal' : 'nowrap', flexShrink: 0, opacity: 0.6, fontSize: 11 }}>
              {displayId}
            </span>
          )}
        </>
      ) : (
        <>
          {showId && (
            <span style={{ marginLeft: 4, whiteSpace: isMobile ? 'normal' : 'nowrap', flexShrink: 0, color: COLORS.heading }}>
              {isDivision && act !== 'cases' && act !== 'rulings' && act !== 'tax-cases' ? `Division ${displayId}` : displayId}
            </span>
          )}
          {displayTitle && (
            <span style={{ marginLeft: showId ? 6 : 4, opacity: 0.85, fontWeight: 400, whiteSpace: isMobile ? 'normal' : 'nowrap', overflow: 'hidden', textOverflow: isMobile ? 'clip' : 'ellipsis' }}>
              {showId ? `— ${displayTitle}` : displayTitle}
            </span>
          )}
        </>
      )}
    </>
  );

  return (
    <div id={`tree-node-${displayId}`} style={{ marginLeft: indent }}>
      <div style={{ display: 'flex', alignItems: 'center' }}>
        {navigable ? (
          <Link to={href as string} onClick={rowClick} style={rowStyle}>
            {rowContent}
          </Link>
        ) : (
          <div onClick={rowOnClick} style={rowStyle}>
            {rowContent}
          </div>
        )}
        {isSection && (node as Section).ato_url && (
          <a
            href={(node as Section).ato_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            aria-label="View on ATO website"
            title="View on ATO website"
            style={{ marginLeft: 6, flexShrink: 0, color: COLORS.textMuted, textDecoration: 'none', fontSize: 11 }}
            onMouseEnter={(e) => { e.currentTarget.style.color = COLORS.accent }}
            onMouseLeave={(e) => { e.currentTarget.style.color = COLORS.textMuted }}
          >
            ↗
          </a>
        )}
      </div>
      {expanded && hasChildren && (
        <div>
          {isPart && ((node as Part).divisions || []).map(d => (
            <TreeNode key={d.id} node={d} level={level + 1} activeSection={activeSection} onSelect={onSelect} getHref={getHref} isMobile={isMobile} expandedIds={expandedIds} act={act} />
          ))}
          {isPart && ((node as Part).sections || []).map(s => (
            <TreeNode key={s.id} node={s} level={level + 1} activeSection={activeSection} onSelect={onSelect} getHref={getHref} isMobile={isMobile} expandedIds={expandedIds} act={act} />
          ))}
          {isDivision && ((node as Division).sections || []).map(s => (
            <TreeNode key={s.id} node={s} level={level + 1} activeSection={activeSection} onSelect={onSelect} getHref={getHref} isMobile={isMobile} expandedIds={expandedIds} act={act} />
          ))}
          {isDivision && ((node as Division).subdivisions || []).map(s => (
            <TreeNode key={s.id} node={s} level={level + 1} activeSection={activeSection} onSelect={onSelect} getHref={getHref} isMobile={isMobile} expandedIds={expandedIds} act={act} />
          ))}
          {isSubdivision && ((node as Subdivision).sections || []).map(s => (
            <TreeNode key={s.id} node={s} level={level + 1} activeSection={activeSection} onSelect={onSelect} getHref={getHref} isMobile={isMobile} expandedIds={expandedIds} act={act} />
          ))}
        </div>
      )}
    </div>
  );
}

// Flatten tree into linear section list for next/prev navigation
function flattenTree(tree: Tree | null): Array<{ id: string; title: string; partId?: string }> {
  if (!tree) return [];
  const result: Array<{ id: string; title: string; partId?: string }> = [];
  for (const part of tree.parts) {
    if ('sections' in part) {
      for (const sec of (part as Part).sections || []) {
        result.push({ id: sec.id, title: sec.title, partId: part.id });
      }
      for (const div of (part as Part).divisions || []) {
        for (const sec of div.sections || []) {
          result.push({ id: sec.id, title: sec.title, partId: part.id });
        }
        for (const sub of div.subdivisions || []) {
          for (const sec of sub.sections || []) {
            result.push({ id: sec.id, title: sec.title, partId: part.id });
          }
        }
      }
    }
  }
  return result;
}

// Find all ancestor IDs that contain the active section
function findExpandedIds(tree: Tree | null, activeSection: string): Set<string> {
  const ids = new Set<string>();
  if (!tree || !activeSection) return ids;
  for (const part of tree.parts) {
    if ('sections' in part) {
      const p = part as Part;
      for (const sec of p.sections || []) {
        if (sec.id === activeSection) {
          ids.add(p.id);
          return ids;
        }
      }
      for (const div of p.divisions || []) {
        for (const sec of div.sections || []) {
          if (sec.id === activeSection) {
            ids.add(p.id);
            ids.add(div.id);
            return ids;
          }
        }
        for (const sub of div.subdivisions || []) {
          for (const sec of sub.sections || []) {
            if (sec.id === activeSection) {
              ids.add(p.id);
              ids.add(div.id);
              ids.add(sub.id);
              return ids;
            }
          }
        }
      }
    }
  }
  return ids;
}

export { TreeNode, flattenTree, findExpandedIds };

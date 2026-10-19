/* Authorized RFC data is projected into workspace views without changing tracking. */
export function linkedCriteria(criteria = [], featureIDs = []) {
  const selected = new Set(featureIDs);
  return criteria.filter(criterion => {
    const ids = criterion.feature_ids || criterion.features || (criterion.feature_id ? [criterion.feature_id] : []);
    return ids.some(id => selected.has(id));
  });
}
export function filterWork(features = [], {query = '', track = '', status = '', owner = '', removed = false} = {}) {
  const term = query.trim().toLocaleLowerCase();
  return features.filter(feature => Boolean(feature.dropped) === removed
    && (!track || feature.track === track)
    && (!status || (feature.implementation || feature.state || 'planned') === status)
    && (!owner || (owner === '__unassigned' ? !feature.owner : feature.owner === owner))
    && (!term || `${feature.id} ${feature.title} ${feature.owner || ''} ${feature.track || ''}`.toLocaleLowerCase().includes(term)));
}
export function workspaceDefaults() {
  return {tab: 'work', mode: 'graph', query: '', track: '', status: '', owner: '', removed: false,
    side: 'details', collapsed: true, selected: '', graph: '', viewport: []};
}

const WORKSPACE_PREFIX = "imrfc.workspace.v1:";
function cleanCheckpoint(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  const clean = {};
  for (const key of ["tab", "mode", "query", "track", "status", "owner", "side", "selected", "graph"]) {
    if (typeof input[key] === "string") clean[key] = input[key].slice(0, key === "query" || key === "graph" ? 2000 : 256);
  }
  for (const key of ["removed", "collapsed", "filtersOpen"]) if (typeof input[key] === "boolean") clean[key] = input[key];
  if (Array.isArray(input.selection)) clean.selection = [...new Set(input.selection.filter(id => typeof id === "string" && id.length <= 200))].slice(0, 200);
  if (Array.isArray(input.viewport)) clean.viewport = input.viewport.slice(0, 100).map(position => {
    const number = (value, fallback, min, max) => typeof value === "number" && Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : fallback;
    return {left: number(position?.left, 0, 0, 1000000), top: number(position?.top, 0, 0, 1000000), scale: number(position?.scale, 1, .25, 2.5)};
  });
  return clean;
}
/** Tab state is restored by callers only after the service authorizes the RFC. */
function createScopedCheckpoint({storage} = {}, prefix, clean) {
  const access = () => storage || globalThis.sessionStorage;
  const key = scope => scope?.user_id && scope?.repo_id && scope?.rfc_id
    ? prefix + JSON.stringify([scope.user_id, scope.repo_id, scope.rfc_id]) : null;
  return {
    restore(scope) {
      const identity = key(scope); if (!identity) return null;
      try { return clean(JSON.parse(access().getItem(identity) || "null")); } catch { return null; }
    },
    save(scope, value) {
      const identity = key(scope), projected = clean(value); if (!identity || projected === null) return;
      try { access().setItem(identity, JSON.stringify(projected)); } catch { /* Storage restrictions cannot block work. */ }
    },
    forget(scope) {
      if (!scope?.user_id || !scope?.rfc_id) return;
      try {
        const store = access();
        for (let index = store.length - 1; index >= 0; index--) {
          const candidate = store.key(index); if (!candidate?.startsWith(prefix)) continue;
          const [user, repo, rfc] = JSON.parse(candidate.slice(prefix.length));
          if (user === scope.user_id && rfc === scope.rfc_id && (!scope.repo_id || repo === scope.repo_id)) store.removeItem(candidate);
        }
      } catch {}
    },
    clear() {
      try { const store = access(); for (let index = store.length - 1; index >= 0; index--) { const candidate = store.key(index); if (candidate?.startsWith(prefix)) store.removeItem(candidate); } } catch {}
    },
  };
}

export function createWorkspaceCheckpoint(options) {
  return createScopedCheckpoint(options, WORKSPACE_PREFIX, cleanCheckpoint);
}
/** Only the unsent composer text is checkpointed, never conversations or tools. */
export function createChatInputCheckpoint(options) {
  return createScopedCheckpoint(options, "imrfc.chat-input.v1:", input => typeof input === "string" ? input.slice(0, 20000) : null);
}

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

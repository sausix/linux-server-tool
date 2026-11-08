"use strict";
function displayHostPath(value) {
  if (typeof value !== 'string') return value;
  let mappings;
  try { mappings = JSON.parse(document.body.dataset.hostPaths || '{}'); } catch { mappings = {}; }
  for (const [source, destination] of Object.entries(mappings).sort(([left], [right]) => right.length - left.length)) {
    const prefix = source.replace(/\/+$/, ''); if (!prefix) continue;
    const escaped = prefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pattern = new RegExp('(?<![\\w/.-])' + escaped + '(?=/|$|[\\s:)\\]}"\'])', 'g');
    value = value.replace(pattern, (match, offset, full) => full[offset + match.length] === '/' ? destination.replace(/\/+$/, '') : destination);
  }
  return value;
}

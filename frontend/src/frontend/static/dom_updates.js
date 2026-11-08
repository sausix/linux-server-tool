"use strict";

/* Keep existing DOM nodes, focus, open details and selections whenever possible. */
function reconcileChildren(parent, desired) {
  const key = item => item.nodeType === 1 ? item.getAttribute('data-key') : null;
  const compatible = (left, right) => left && left.nodeType === right.nodeType && left.nodeName === right.nodeName;
  const keyed = new Map([...parent.childNodes].filter(item => key(item)).map(item => [key(item), item]));
  const retained = new Set();
  for (const [index, fresh] of [...desired.childNodes].entries()) {
    const atIndex = parent.childNodes[index];
    let current = key(fresh) ? keyed.get(key(fresh)) : (!key(atIndex || {}) ? atIndex : null);
    if (!compatible(current, fresh)) current = null;
    if (!current) {
      parent.insertBefore(fresh, atIndex || null);
      retained.add(fresh);
      continue;
    }
    retained.add(current);
    if (current.nodeType === 3) {
      if (current.nodeValue !== fresh.nodeValue) {
        const selection = window.getSelection();
        // Defer changing selected text (e.g. counters in Raw) until selection clears.
        if (!selection || selection.isCollapsed || !selection.containsNode(current, true)) current.nodeValue = fresh.nodeValue;
      }
    } else {
      for (const attribute of [...current.attributes]) {
        if (current.nodeName === 'DETAILS' && attribute.name === 'open') continue;
        if (!fresh.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
      }
      for (const attribute of [...fresh.attributes]) {
        if (current.nodeName === 'DETAILS' && attribute.name === 'open') continue;
        if (current.getAttribute(attribute.name) !== attribute.value) current.setAttribute(attribute.name, attribute.value);
      }
      reconcileChildren(current, fresh);
    }
    if (parent.childNodes[index] !== current) parent.insertBefore(current, parent.childNodes[index] || null);
  }
  for (const child of [...parent.childNodes]) if (!retained.has(child)) parent.removeChild(child);
}

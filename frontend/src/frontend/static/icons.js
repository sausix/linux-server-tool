"use strict";
function appIcon(name) {
  const namespace = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(namespace, 'svg'); svg.setAttribute('class', 'docker-icon');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
  const use = document.createElementNS(namespace, 'use');
  use.setAttribute('href', (document.body.dataset.iconsUrl || '/static/icons.svg') + '#' + name);
  svg.append(use); return svg;
}

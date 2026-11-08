"use strict";
const creation = document.querySelector('[data-stack-create]');
if (creation) {
  const sync = () => {
    const mode = creation.querySelector('input[name="mode"]:checked').value;
    for (const panel of creation.querySelectorAll('[data-mode-panel]')) panel.hidden = panel.dataset.modePanel !== mode;
    creation.querySelector('[name="upload"]').required = mode === 'upload';
    creation.querySelector('[data-editor-text]').disabled = mode === 'upload';
  };
  creation.addEventListener('change', sync); sync();
}

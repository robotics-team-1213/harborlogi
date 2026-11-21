import {mountTopview} from './topview.mjs';

const dialog = document.getElementById('intake-dialog');
if (dialog) {
  const body = dialog.querySelector('[data-intake-dialog-body]');
  const title = document.getElementById('intake-dialog-title');
  let opener = null;
  let cleanups = [];

  const closeDialog = () => {
    cleanups.splice(0).forEach(fn => fn());
    if (dialog.open) dialog.close();
    if (body) body.replaceChildren();
    if (opener && typeof opener.focus === 'function') opener.focus();
    opener = null;
  };

  dialog.addEventListener('click', event => {
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (
      event.clientX < rect.left || event.clientX > rect.right ||
      event.clientY < rect.top || event.clientY > rect.bottom
    )) {
      closeDialog();
    }
    if (event.target.closest('[data-intake-close]')) {
      closeDialog();
    }
  });
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    closeDialog();
  });

  document.querySelectorAll('[data-intake-dialog]').forEach(link => {
    link.addEventListener('click', async event => {
      event.preventDefault();
      opener = link;
      if (title) title.textContent = `${link.dataset.intakeRef || ''} 반입 비교`;
      const url = `${link.dataset.intakeDialog}?fragment=1`;
      try {
        const response = await fetch(url, {credentials: 'same-origin'});
        if (!response.ok) {
          window.location.href = link.href;
          return;
        }
        cleanups.splice(0).forEach(fn => fn());
        if (body) body.innerHTML = await response.text();
        cleanups = [...(body?.querySelectorAll('[data-topview-url]') || [])].map(mountTopview).filter(Boolean);
        if (!dialog.open) dialog.showModal();
      } catch (_) {
        window.location.href = link.href;
      }
    });
  });
}

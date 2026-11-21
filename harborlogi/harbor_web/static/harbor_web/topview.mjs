export function pollDelay(status, hidden) {
  return hidden ? null : status === 200 ? 1000 : 3000;
}

export function frameStatus(status, capturedAt) {
  if (status === 404) return '그림이 오래됨';
  if (status !== 200) return 'ROSY 연결 안 됨';
  if (!capturedAt) return '';
  const captured = new Date(capturedAt);
  if (Number.isNaN(captured.getTime())) return '';
  const time = [captured.getHours(), captured.getMinutes(), captured.getSeconds()]
    .map(value => String(value).padStart(2, '0')).join(':');
  return '찍은 시각 ' + time;
}

export function mountTopview(panel) {
  const image = panel.querySelector('[data-topview-image]');
  const statusText = panel.querySelector('[data-topview-status]');
  const select = panel.querySelector('[data-topview-source]');
  let timer, request, imageUrl;

  function clearImage() {
    image.hidden = true;
    image.removeAttribute('src');
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
  }

  function stop() {
    clearTimeout(timer);
    request?.abort();
    request = null;
  }

  async function refresh() {
    if (document.hidden) return;
    const current = new AbortController();
    request = current;
    let status = 0;
    try {
      const response = await fetch(select?.value || panel.dataset.topviewUrl, {
        cache: 'no-store', signal: current.signal, credentials: 'same-origin',
      });
      if (request !== current) return;
      status = response.status;
      if (response.ok && response.headers.get('Content-Type')?.split(';')[0] === 'image/jpeg') {
        const blob = await response.blob();
        if (request !== current) return;
        clearImage();
        imageUrl = URL.createObjectURL(blob);
        image.src = imageUrl;
        image.hidden = false;
      } else {
        if (status === 200) status = 502;
        clearImage();
      }
      statusText.textContent = frameStatus(status, response.headers.get('X-Frame-Captured-At'));
    } catch (_) {
      if (request !== current) return;
      status = 0;
      clearImage();
      statusText.textContent = frameStatus(status);
    } finally {
      if (request === current) {
        request = null;
        const delay = pollDelay(status, document.hidden);
        if (delay !== null) timer = setTimeout(refresh, delay);
      }
    }
  }

  function restart() {
    stop();
    refresh();
  }
  select?.addEventListener('change', () => { clearImage(); restart(); });
  document.addEventListener('visibilitychange', restart);
  window.addEventListener('pagehide', () => { stop(); clearImage(); });
  window.addEventListener('pageshow', event => { if (event.persisted) restart(); });
  refresh();
  return () => {
    stop();
    clearImage();
    document.removeEventListener('visibilitychange', restart);
  };
}

if (typeof document !== 'undefined') {
  document.querySelectorAll('[data-topview-url]').forEach(mountTopview);
}

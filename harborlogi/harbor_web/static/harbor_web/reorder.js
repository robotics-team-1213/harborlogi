import {applyReasonText, buildLaneStrings, frontIndex, moveItem} from './reorder-model.mjs';

const UNDO_KEY = 'harbor:work-reorder-undo';

function mountReorderEditor(editor) {
  const list = editor.querySelector('#wk-flow');
  const actions = editor.querySelector('#wk-out-actions');
  const help = editor.querySelector('#wk-edit-help');
  const resultBox = editor.querySelector('#wk-result');
  const form = editor.querySelector('form[data-reorder-form]');
  if (!list || !actions || !resultBox || !form) return;

  const validateUrl = editor.dataset.validateUrl;
  const reorderUrl = editor.dataset.reorderUrl;
  const csrf = form.querySelector('input[name="csrfmiddlewaretoken"]')?.value || '';
  const readItems = () => [...list.querySelectorAll('[data-order]')].map(li => ({
    id: li.dataset.order,
    version: Number(li.dataset.version),
    tier: Number(li.dataset.tier || 1),
    code: li.dataset.code,
    running: li.dataset.running === '1',
    node: li,
  }));

  const currentItems = readItems();
  let editing = false;
  let draft = null;
  let validation = null;
  let busy = false;

  const readUndo = () => {
    try {
      const raw = sessionStorage.getItem(UNDO_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  };
  const writeUndo = value => {
    try {
      if (value) sessionStorage.setItem(UNDO_KEY, JSON.stringify(value));
      else sessionStorage.removeItem(UNDO_KEY);
    } catch (_) {}
  };

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'text') node.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
      else node.setAttribute(k, v === true ? '' : String(v));
    }
    for (const child of Array.isArray(children) ? children : [children]) {
      if (child !== null && child !== undefined) node.append(child);
    }
    return node;
  }

  function laneFormData(items, fixedFlags = [true, true, true]) {
    const fd = new FormData();
    if (csrf) fd.append('csrfmiddlewaretoken', csrf);
    for (const value of buildLaneStrings(items, fixedFlags)) {
      fd.append('lane', value);
    }
    return fd;
  }

  async function postValidate(items) {
    const resp = await fetch(validateUrl, {
      method: 'POST',
      body: laneFormData(items),
      credentials: 'same-origin',
      headers: {'X-CSRFToken': csrf},
    });
    const data = await resp.json().catch(() => ({ok: false, reasons: ['순서 검증 응답을 읽지 못했습니다.']}));
    return {status: resp.status, data};
  }

  async function postApply(items, previousIds = null) {
    const resp = await fetch(reorderUrl, {
      method: 'POST',
      body: laneFormData(items),
      credentials: 'same-origin',
      headers: {'X-CSRFToken': csrf},
    });
    if (!resp.ok) {
      return {ok: false, status: resp.status};
    }
    writeUndo(previousIds ? {ids: previousIds} : null);
    window.location.reload();
    return {ok: true};
  }

  function reorderListNodes(items) {
    items.forEach((item, index) => {
      const num = item.node.querySelector('[data-reorder-seq]');
      if (num) num.textContent = String(index + 1);
      list.append(item.node);
    });
  }

  function wireDrag(li, item) {
    let target = null;
    const clearMarks = () => list.querySelectorAll('.is-drop-before,.is-drop-after')
      .forEach(n => n.classList.remove('is-drop-before', 'is-drop-after'));
    li.onpointerdown = event => {
      if (!editing || item.running || event.button > 0 || event.target.closest('button, a')) return;
      event.preventDefault();
      li.setPointerCapture(event.pointerId);
      li.classList.add('is-dragging');
      target = null;
    };
    li.onpointermove = event => {
      if (!li.classList.contains('is-dragging')) return;
      let best = null, bestDist = Infinity;
      for (const candidate of list.querySelectorAll('[data-order]')) {
        if (candidate === li || candidate.dataset.running === '1') continue;
        const r = candidate.getBoundingClientRect();
        const d = Math.hypot(event.clientX - (r.left + r.width / 2), event.clientY - (r.top + r.height / 2));
        if (d < bestDist) { bestDist = d; best = candidate; }
      }
      clearMarks();
      if (!best) return;
      const r = best.getBoundingClientRect();
      const before = event.clientX < r.left + r.width / 2;
      best.classList.add(before ? 'is-drop-before' : 'is-drop-after');
      target = {id: best.dataset.order, before};
    };
    const end = () => {
      if (!li.classList.contains('is-dragging')) return;
      li.classList.remove('is-dragging');
      clearMarks();
      if (!target) return;
      const idx = draft.findIndex(x => x.id === target.id);
      if (idx === -1) return;
      const fromIdx = draft.findIndex(x => x.id === item.id);
      let dest = target.before ? idx : idx + 1;
      if (fromIdx < dest) dest -= 1;
      draft = moveItem(draft, item.id, dest);
      validation = null;
      render();
    };
    li.onpointerup = end;
    li.onpointercancel = end;
  }

  function moveBy(id, delta) {
    const idx = draft.findIndex(item => item.id === id);
    if (idx === -1) return;
    draft = moveItem(draft, id, idx + delta);
    validation = null;
    render();
    list.querySelector(`[data-order="${id}"] .work-chip`)?.focus();
  }

  function moveToFront(id) {
    draft = moveItem(draft, id, frontIndex(draft));
    validation = null;
    render();
    list.querySelector(`[data-order="${id}"] .work-chip`)?.focus();
  }

  async function runValidate() {
    if (busy || !draft) return;
    busy = true;
    render();
    const {data} = await postValidate(draft);
    validation = data;
    busy = false;
    render();
  }

  async function runApply() {
    if (busy || !draft || !validation?.ok) return;
    busy = true;
    render();
    const recheck = await postValidate(draft);
    validation = recheck.data;
    if (!validation.ok) {
      busy = false;
      render();
      return;
    }
    const previousIds = currentItems.map(item => ({id: item.id, tier: item.tier}));
    const applied = await postApply(draft, previousIds);
    if (!applied.ok) {
      validation = {ok: false, reasons: ['반출 대기열이 바뀌었습니다. 새로고침 후 다시 시도하세요.']};
      busy = false;
      render();
    }
  }

  async function runUndo() {
    const saved = readUndo();
    if (busy || !saved?.ids) return;
    const byId = new Map(currentItems.map(item => [item.id, item]));
    const restored = saved.ids.map(entry => {
      const cur = byId.get(entry.id);
      return cur ? {...cur, tier: entry.tier} : null;
    }).filter(Boolean);
    if (restored.length !== currentItems.length) {
      writeUndo(null);
      render();
      return;
    }
    busy = true;
    render();
    const check = await postValidate(restored);
    if (!check.data.ok) {
      writeUndo(null);
      editing = true;
      draft = restored;
      validation = check.data;
      busy = false;
      render();
      return;
    }
    const done = await postApply(restored, null);
    if (!done.ok) {
      busy = false;
      render();
    }
  }

  function render() {
    const items = editing ? draft : currentItems;
    list.classList.toggle('is-edit', editing);
    reorderListNodes(items);
    const minFront = frontIndex(items);

    items.forEach((item, index) => {
      const li = item.node;
      const chip = li.querySelector('.work-chip');
      li.querySelectorAll('.wk-front,.wk-move').forEach(btn => btn.remove());
      if (editing && !item.running) {
        if (chip) {
          chip.setAttribute('tabindex', '0');
          chip.onkeydown = event => {
            if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') { event.preventDefault(); moveBy(item.id, -1); }
            if (event.key === 'ArrowRight' || event.key === 'ArrowDown') { event.preventDefault(); moveBy(item.id, 1); }
          };
        }
        if (index !== minFront) {
          li.append(el('button', {
            type: 'button',
            class: 'quiet wk-front',
            disabled: busy,
            onClick: () => moveToFront(item.id),
          }, '맨 앞으로'));
        }
        wireDrag(li, item);
      } else {
        if (chip) {
          chip.removeAttribute('tabindex');
          chip.onkeydown = null;
        }
        li.onpointerdown = null;
        li.onpointermove = null;
        li.onpointerup = null;
        li.onpointercancel = null;
      }
    });

    if (help) help.hidden = !editing;
    actions.replaceChildren();
    if (!editing) {
      actions.append(el('button', {
        type: 'button',
        id: 'wk-reorder',
        class: 'quiet',
        onClick: () => {
          editing = true;
          draft = currentItems.map(item => ({...item}));
          validation = null;
          render();
        },
      }, '순서 바꾸기(예외)'));
      if (readUndo()) {
        actions.append(el('span', {class: 'helptext', text: '적용함 ·'}));
        actions.append(el('button', {
          type: 'button',
          class: 'quiet',
          disabled: busy,
          onClick: runUndo,
        }, '되돌리기'));
      }
    } else {
      actions.append(el('button', {
        type: 'button',
        id: 'wk-validate',
        disabled: busy,
        onClick: runValidate,
      }, busy ? '검증 중…' : '이 순서 검증해 보기'));
      actions.append(el('button', {
        type: 'button',
        class: 'quiet',
        disabled: busy,
        onClick: () => { editing = false; draft = null; validation = null; render(); },
      }, '취소'));
    }

    resultBox.replaceChildren();
    if (editing && validation) {
      const ok = Boolean(validation.ok);
      const summary = ok && validation.current && validation.current.moves !== null
        ? `재취급 ${validation.current.rehandles}→${validation.rehandles}회 · 이동 ${validation.current.moves}→${validation.moves}회`
        : ok
          ? `재취급 ${validation.rehandles}회 · 이동 ${validation.moves}회`
          : '이 순서로는 낼 수 없음';
      const panel = el('div', {id: 'wk-result-box', class: `wk-result ${ok ? 'is-good' : 'is-critical'}`}, [
        el('div', {class: 'wk-result-row'}, [
          el('span', {class: `badge ${ok ? 'READY' : 'HOLD'}`, text: ok ? '가능' : '불가'}),
          el('span', {text: summary}),
        ]),
        !ok && validation.reasons?.length
          ? el('ul', {class: 'helptext'}, validation.reasons.map(msg => el('li', {text: msg})))
          : null,
        el('div', {class: 'wk-result-row'}, [
          el('button', {
            type: 'button',
            id: 'wk-apply',
            disabled: !ok || busy,
            'aria-describedby': 'wk-apply-reason',
            onClick: runApply,
          }, '적용'),
          el('span', {id: 'wk-apply-reason', class: 'helptext', text: applyReasonText(validation)}),
        ]),
      ]);
      resultBox.append(panel);
    }
  }

  render();
}

if (typeof document !== 'undefined') {
  document.querySelectorAll('[data-reorder-editor]').forEach(mountReorderEditor);
}

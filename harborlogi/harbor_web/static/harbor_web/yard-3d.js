import {mountLinkedGrid, selectionFor, atPosition, slotLabel} from './yard-selection.mjs';
import {cloneBoxes, transit, moveBox, placementFromWorld} from './yard-model.mjs';
import {DraftSession} from './yard-draft.mjs';

function fallback(figure) {
  const preview = figure.closest('[data-yard-preview]');
  if (document.fullscreenElement === figure) document.exitFullscreen().catch(() => {});
  figure.dataset.ready = 'false'; figure.dataset.renderer = 'unavailable';
  figure.hidden = true;
  preview.querySelector('[data-yard-linked-flat]')?.remove();
  preview.classList.remove('yard-linked');
  preview.querySelector('[data-yard-flat]').hidden = false;
  preview.querySelector('[data-yard-views]').hidden = true;
  preview.querySelector('[data-yard-edit-tools]').hidden = true;
  preview.querySelector('[data-yard-3d-fallback]').hidden = false;
}

export async function mountYardViewer(figure) {
  const {createRenderer} = await import('./yard-renderer.js');
  const preview = figure.closest('[data-yard-preview]');
  const scene = JSON.parse(document.getElementById(figure.dataset.scene).textContent);
  const canvas = figure.querySelector('canvas');
  const flat = preview.querySelector('[data-yard-flat]');
  const views = preview.querySelector('[data-yard-views]');
  const controls = [...preview.querySelectorAll('[data-yard-stage]')];
  const cargo = figure.querySelector('[data-scene-cargo]');
  const info = figure.querySelector('[data-scene-info]');
  const play = figure.querySelector('[data-scene-play]');
  const motion = figure.querySelector('[data-scene-motion]');
  const speed = figure.querySelector('[data-scene-speed]');
  const animateMotion = figure.querySelector('[data-scene-animate]');
  const progress = figure.querySelector('[data-scene-progress]');
  const editor = figure.querySelector('[data-scene-editor]');
  const editTools = preview.querySelector('[data-yard-edit-tools]');
  const editButton = editTools.querySelector('[data-scene-action="edit"]');
  const loadPlan = editTools.querySelector('[data-scene-load-plan]');
  const undo = editTools.querySelector('[data-scene-undo]');
  const redo = editTools.querySelector('[data-scene-redo]');
  const draftState = figure.querySelector('[data-scene-draft]');
  const error = figure.querySelector('[data-scene-error]');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  animateMotion.checked = !reduced.matches;
  const events = new AbortController();
  const on = (node, type, fn) => node.addEventListener(type, fn, {signal: events.signal});
  const palette = getComputedStyle(document.documentElement);
  const token = name => palette.getPropertyValue(name).trim();
  const colors = {target: token('--primitive-color-navy'), blocker: token('--primitive-color-orange'),
    stored: token('--primitive-color-teal'), line: token('--semantic-border-divider'),
    text: token('--semantic-text-primary'), muted: token('--semantic-text-secondary'),
    floor: token('--semantic-surface-panel'), white: token('--primitive-color-white'),
    canvas: token('--semantic-surface-canvas')};
  let index = Number(controls.find(b => b.getAttribute('aria-pressed') === 'true')?.dataset.yardStage || 0);
  let location = null, linked = null;
  let selected = '', current = cloneBoxes(scene.frames[index].boxes), draft = null;
  let playing = false, internalStep = false, isolated = false, timer = 0, raf = 0, animation = null;
  let destroyed = false, draftHistory = [], replayIndex = 0, session = null;
  const ceiling = Math.max(...scene.areas.map(a => a.levels)) + .6;
  let renderer = null;
  renderer = createRenderer(canvas, scene.areas, {
    showVolumes: scene.kind === 'map-definition',
    colors, onSelect: id => select(id), onTransform: (id, position) => {
      if (!draft || playing) return;
      const box = draft.find(b => b.id === id);
      const placement = placementFromWorld(scene.areas, box, position);
      if (placement) applyDraft(id, placement);
      else { error.textContent = '구역 안의 격자 위치로 옮겨주세요.'; renderer.setBoxes(draft); }
    }, onError: () => { destroy(); fallback(figure); },
  });
  const slotCells = scene.slot_cells || 0;  // a site with fixed 1×3 slots (KAN-115) names slots, not cells
  function stop() {
    if (draft && animation) {
      progress.dataset.phase = '중지 · 최종 가상 배치'; progress.dataset.progress = '100';
      progress.textContent = '로컬 가상 변경 재생 중지 · 최종 가상 배치 복원 · 서버에 저장되지 않음';
    }
    playing = false; clearTimeout(timer); cancelAnimationFrame(raf); raf = 0; animation = null;
    canvas.dataset.moving = 'false';
    play.textContent = draft ? '가상 변경 재생' : '계획 재생'; play.setAttribute('aria-pressed', 'false');
    editButton.disabled = scene.kind === 'map-definition';
    if (draft && !destroyed) { renderer?.setEditable(true); editor.querySelector('fieldset').disabled = !selected; paint(draft); }
    updateDraftState();
  }
  function status(text) { motion.textContent = text; }
  function boxes() { return draft || scene.frames[index].boxes; }
  function changedCount() {
    return session ? session.changes.length : 0;
  }
  function updateDraftState() {
    draftState.hidden = !draft;
    draftState.textContent = draft ? '가상 배치 · 변경 ' + changedCount() + '개 · 서버에 저장되지 않음' : '';
    figure.dataset.editing = String(Boolean(draft));
    figure.dataset.draftChanges = String(changedCount());
    undo.disabled = !session || !session.cursor || playing;
    redo.disabled = !session || session.cursor === session.history.length - 1 || playing;
    loadPlan.disabled = Boolean(draft) || playing;
    editTools.querySelector('[data-scene-change-panel]').hidden = !draft;
    editTools.querySelector('[data-scene-change-count]').textContent = String(changedCount());
    const changes = editTools.querySelector('[data-scene-changes]');
    changes.replaceChildren();
    for (const {before,after} of session?.changes || []) {
      const item = document.createElement('li');
      item.textContent = `${after.id} · ${before.positions.join(' / ')} → ${after.positions.join(' / ')}`;
      changes.append(item);
    }
    editTools.querySelector('[data-scene-edit-help]').textContent = draft ?
      `${selected || '화물 미선택'} · 화물을 고른 뒤 평면의 ‘여기로 이동’ 칸을 누르세요. 이동 불가 칸을 누르면 이유를 확인합니다.` :
      '화물을 선택하고 가상 배치 편집을 시작하세요. 저장된 계획 결과도 불러와 수정할 수 있습니다.';
  }
  const selection = () => selectionFor(boxes().find(b => b.id === selected)) || location;
  const mode = () => scene.kind === 'map-definition' ? 'map-definition' : draft ? 'local-draft' : index ? 'predicted' : 'original';
  function syncLinked() {
    linked?.update(boxes(), mode(), selection(), scene.frames[index].label);
    renderer.selectLocation(location);
    preview.dispatchEvent(new CustomEvent('yardselectionchange', {detail:{selection:selection(), source:mode(), index}}));
  }
  function details() {
    const box = boxes().find(b => b.id === selected);
    const base = scene.frames[index].boxes.find(b => b.id === selected);
    cargo.value = box ? box.id : '';
    // With fixed slots the size, turn and covered cells never change, so only the slot is named.
    const where = box && (slotCells ? (draft ? '가상: ' + slotLabel(scene.areas, box, slotCells) + ' · 기준: ' + slotLabel(scene.areas, base, slotCells) : slotLabel(scene.areas, box, slotCells))
      : box.size + ' · ' + box.orientation + '도 · ' + (draft ? '가상: ' + box.area + ' / ' + box.row + '행 ' + box.column + '열 ' + box.level + '층 · 기준: ' + base.positions.join(' / ') : box.positions.join(' / ')));
    info.textContent = box ? box.id + ' · ' + box.label + (!draft && scene.rehandle?.includes(box.id) ? ' (재취급 예정 · 계획 기준)' : '') + ' · ' + where
      : location ? `${scene.kind === 'map-definition' ? '미관측 위치 · 점유 확인 안 됨' : slotCells ? '빈 자리' : '빈 위치'} · ${slotCells ? slotLabel(scene.areas, location, slotCells) : `${location.area} · ${location.row}행 ${location.column}열 ${location.level}층`} · 화물 아님`
        : `박스나 화물 목록을 선택해 ${slotCells ? '' : '크기·'}위치를 확인하세요.`;
    if (location && scene.kind === 'map-definition') status(info.textContent);
    for (const name of ['area','row','column','level','orientation']) editor.elements[name].value = box ? box[name] : '';
    editor.querySelector('fieldset').disabled = !box || playing;
    figure.querySelector('[data-scene-action="focus"]').disabled = !box;
    figure.querySelector('[data-scene-action="isolate"]').disabled = !box;
    updateDraftState();
  }
  function select(id) {
    location = null;
    selected = id || ''; renderer.select(selected);
    if (!selected && isolated) {
      isolated = false; figure.querySelector('[data-scene-action="isolate"]').setAttribute('aria-pressed', 'false'); renderer.isolate(null);
    }
    if (isolated) renderer.isolate(selected || null);
    details(); syncLinked();
  }
  function fillCargo() {
    cargo.replaceChildren(new Option('화물 선택', ''));
    boxes().forEach(box => cargo.add(new Option(box.id + (slotCells ? '' : ' · ' + box.size) + ' · ' + box.label, box.id)));
    details();
  }
  function paint(values) {
    current = cloneBoxes(values); renderer.setBoxes(current);
    canvas.dataset.boxCount = String(current.length); canvas.dataset.frame = String(index);
  }
  function applyDraft(id, placement) {
    if (playing) return;
    const result = session.move(id, placement);
    error.textContent = result.valid ? '' : result.reason;
    if (result.valid) {
      draftHistory = session.frames;
      play.disabled = draftHistory.length < 2;
      draft = cloneBoxes(session.boxes); status(id + ' 가상 위치 변경 · 논리 점유·지지 검사 통과');
    }
    paint(draft);
    if (result.valid) select(id); else details();
    linked?.feedback(result.valid ? `${id} 이동 완료 · ${draft.find(box=>box.id===id).positions.join(' / ')} · 되돌리기로 복원할 수 있습니다.` : result.reason);
  }
  function lockDraft(value) {
    controls.forEach(button => { button.disabled = value; });
    play.disabled = value && draftHistory.length < 2;
    play.hidden = scene.kind === 'map-definition' || (!value && scene.frames.length < 2);
    play.textContent = value ? '가상 변경 재생' : '계획 재생';
    editButton.setAttribute('aria-pressed', String(value));
    editButton.textContent = value ? '원본 복원·편집 끝내기' : '가상 배치 편집';
    editor.hidden = !value; renderer.setEditable(value);
  }
  function toggleEdit(usePlan = false) {
    stop(); cancelAnimationFrame(raf); animation = null; canvas.dataset.moving = 'false';
    if (draft) {
      draft = null; session = null; draftHistory = []; lockDraft(false); paint(scene.frames[index].boxes);
      status('가상 변경을 버리고 선택한 기준 배치를 복원했습니다.');
    } else {
      if (usePlan) controls[0]?.click();
      const next = new DraftSession(scene.areas,scene.frames[index].boxes);
      if (usePlan) {
        const result = next.load(scene.frames.map(frame=>frame.boxes));
        if (!result.valid) { error.textContent = result.reason; return; }
      }
      session = next; draft = cloneBoxes(session.boxes); draftHistory = session.frames; lockDraft(true); paint(draft);
      selected ||= draft.find(box=>box.role==='target')?.id || draft[0]?.id || '';
      status(usePlan ? '저장된 이동 순서와 결과를 불러왔습니다. 되돌리거나 이어서 편집하세요.' : '목적 칸 클릭·3D 이동 핸들·좌표 입력으로 배치하세요.');
    }
    error.textContent = ''; fillCargo(); select(selected);
    showFlat(flatView);
  }
  function advance(delay = 550) {
    if (!playing) return;
    timer = setTimeout(() => {
      if (!playing || document.hidden || figure.hidden) return;
      if (draft) {
        if (replayIndex >= draftHistory.length - 1) { stop(); status('로컬 가상 변경 재생 완료 · 서버에 저장되지 않음'); return; }
        startTransition(draftHistory[++replayIndex]);
        return;
      }
      if (index === scene.frames.length - 1) { stop(); status('계획 재생 완료 · 실제 실행 상태는 바뀌지 않았습니다.'); return; }
      internalStep = true; controls[index + 1].click(); internalStep = false;
    }, delay);
  }
  function describeMove(segment, t) {
    const {from, to} = segment;
    const location = box => box.area + ' ' + box.row + '행 ' + box.column + '열 ' + box.level + '층';
    const phase = !from ? '배치 비교 · 관측 출발 위치 없음' : segment.instant ? '방향 비교 · 즉시 전환' : t < .25 ? '들어 올림' : t < .75 ? '이동' : t < 1 ? '내려놓기' : '완료';
    Object.assign(progress.dataset, {cargo:to.id, source:from ? location(from) : '관측 출발 위치 없음',
      destination:location(to), phase, progress:String(Math.round(t * 100)), mode:draft ? 'local-draft' : 'plan'});
    progress.textContent = (draft ? '로컬 가상 변경' : '계획 설명') + ' · ' + to.id + ' · ' + progress.dataset.source + ' → ' + location(to) +
      ' · ' + phase + ' · ' + Math.round(t * 100) + '%';
  }
  function startTransition(target) {
    const from = new Map(current.map(box => [box.id, box]));
    const moved = target.filter(to => {
      const initial = from.get(to.id);
      return !initial || ['x','y','z','orientation'].some(key => initial[key] !== to[key]);
    });
    const segments = [], working = cloneBoxes(target.filter(to => from.has(to.id)).map(to => from.get(to.id)));
    for (const to of moved) {
      const initial = from.get(to.id);
      if (!initial) working.push(cloneBoxes([to])[0]);
      segments.push({from:initial, to, boxes:cloneBoxes(working),
        instant:initial && ['x','y','z'].every(key => initial[key] === to[key])});
      working[working.findIndex(box => box.id === to.id)] = cloneBoxes([to])[0];
    }
    if (!segments.length || !animateMotion.checked || figure.hidden || document.hidden) {
      paint(target); canvas.dataset.moving = 'false';
      if (segments.length) describeMove(segments.at(-1), 1);
      status(draft ? '로컬 가상 배치 비교 · 즉시 전환' : scene.frames[index].label + ' · 예상 배치');
      advance(!animateMotion.checked ? 4500 / Number(speed.value) : 550); return;
    }
    animation = {segments, segment:0, target, elapsed:0, last:performance.now()};
    play.textContent = '재생 중지'; play.setAttribute('aria-pressed', 'true');
    canvas.dataset.moving = String(Boolean(segments[0].from && !segments[0].instant));
    describeMove(segments[0], 0);
    status('배치 이동 설명 · 실제 로봇 경로·작업 시간이 아님');
    raf = requestAnimationFrame(tick);
  }
  function tick(now) {
    raf = 0;
    if (!animation || destroyed) return;
    animation.elapsed += (now - animation.last) * Number(speed.value); animation.last = now;
    const segment = animation.segments[animation.segment];
    const t = segment.instant ? 1 : Math.min(1, animation.elapsed / 4500);
    paint(segment.boxes.map(box => box.id === segment.to.id && segment.from ? transit(segment.from, segment.to, t, ceiling) : box));
    describeMove(segment, t);
    if (t < 1) raf = requestAnimationFrame(tick);
    else if (++animation.segment < animation.segments.length) {
      animation.elapsed = 0;
      const next = animation.segments[animation.segment];
      canvas.dataset.moving = String(Boolean(next.from && !next.instant));
      describeMove(next, 0); raf = requestAnimationFrame(tick);
    } else {
      paint(animation.target); animation = null; canvas.dataset.moving = 'false';
      if (!playing) { play.textContent = draft ? '가상 변경 재생' : '계획 재생'; play.setAttribute('aria-pressed', 'false'); }
      status(draft ? '로컬 가상 변경 · 서버에 저장되지 않음' : scene.frames[index].label + ' · 예상 배치'); advance();
    }
  }
  function selectFrame(next, animate = true) {
    if (draft || !scene.frames[next]) return;
    if (!internalStep) stop();
    cancelAnimationFrame(raf); raf = 0; animation = null;
    index = next;
    if (selected && !boxes().some(box => box.id === selected)) selected = '';
    if (location) { const occupant = atPosition(boxes(), location); if (occupant) { selected = occupant.id; location = null; } }
    renderer.select(selected); fillCargo(); syncLinked();
    figure.querySelector('[data-scene-step]').textContent = scene.frames[index].label;
    if (animate && next) startTransition(scene.frames[index].boxes);
    else {
      canvas.dataset.moving = 'false'; paint(scene.frames[index].boxes);
      status(scene.kind === 'map-definition' ? scene.frames[index].label : next ? scene.frames[index].label + ' · 예상 배치' : '원본 관측 배치'); advance();
    }
  }
  const desktop = matchMedia('(min-width: 1200px)');
  let flatView = false;
  function showFlat(value) {
    flatView = value;
    if (!desktop.matches) {
      stop();
      if (!draft) selectFrame(index, false);
    }
    figure.hidden = !desktop.matches && value && !draft;
    figure.classList.toggle('flat-edit', !desktop.matches && value && Boolean(draft));
    if (linked) linked.grid.hidden = !desktop.matches && !value;
    flat.hidden = true;
    views.querySelectorAll('[data-yard-view]').forEach(button => button.setAttribute('aria-pressed', String((button.dataset.yardView === 'flat') === value)));
    if (!figure.hidden) renderer.resize();
  }
  on(desktop, 'change', () => showFlat(flatView));
  linked = mountLinkedGrid(preview, scene, value => {
    if (animation || playing) { stop(); selectFrame(index, false); }
    if (draft && selected && !value.cargoId) {
      const box = draft.find(item=>item.id===selected);
      applyDraft(selected,{...value,orientation:box.orientation});
      return;
    }
    if (value.cargoId) { error.textContent = ''; select(value.cargoId); renderer.fit(value.cargoId); }
    else {
      selected = ''; location = value; renderer.select(null); renderer.isolate(null);
      isolated = false; figure.querySelector('[data-scene-action="isolate"]').setAttribute('aria-pressed', 'false');
      details(); syncLinked(); renderer.fitLocation(value);
    }
  }, position => draft && selected && !playing ? moveBox(scene.areas,draft,selected,
    {...position,orientation:draft.find(box=>box.id===selected).orientation}) : null);
  scene.areas.forEach(area => editor.elements.area.add(new Option(area.label + ' · ' + area.id, area.id)));
  on(editor, 'submit', event => {
    event.preventDefault();
    if (!draft || !selected || playing) return;
    applyDraft(selected, {area: editor.elements.area.value, row: Number(editor.elements.row.value),
      column: Number(editor.elements.column.value), level: Number(editor.elements.level.value),
      orientation: Number(editor.elements.orientation.value)});
  });
  on(cargo, 'change', () => { error.textContent = ''; select(cargo.value); });
  on(editButton, 'click', () => toggleEdit());
  on(loadPlan, 'click', () => { if (!draft && scene.frames.length > 1) toggleEdit(true); });
  for (const [button,action] of [[undo,'undo'],[redo,'redo']]) on(button,'click',()=>{
    if (!session || playing) return;
    stop(); session[action](); draft = cloneBoxes(session.boxes); draftHistory = session.frames;
    play.disabled = draftHistory.length < 2; error.textContent = '';
    paint(draft); select(selected); status(action === 'undo' ? '직전 이동을 되돌렸습니다.' : '이동을 다시 적용했습니다.');
  });
  on(play, 'click', () => {
    if (playing || animation) { stop(); if (!draft) selectFrame(index, false); return; }
    playing = true; play.textContent = '재생 중지'; play.setAttribute('aria-pressed', 'true');
    if (draft) {
      replayIndex = 0; paint(draftHistory[0]); renderer.setEditable(false);
      editor.querySelector('fieldset').disabled = true; editButton.disabled = true; updateDraftState(); advance(); return;
    }
    if (index === scene.frames.length - 1) { internalStep = true; controls[0].click(); internalStep = false; }
    else if (!animation) advance();
  });
  figure.querySelectorAll('[data-scene-camera]').forEach(button => on(button, 'click', () => renderer.view(button.dataset.sceneCamera)));
  figure.querySelectorAll('[data-scene-zoom]').forEach(button => on(button, 'click', () => renderer.zoom(Number(button.dataset.sceneZoom) > 0 ? 1.22 : .82)));
  views.querySelectorAll('[data-yard-view]').forEach(button => on(button, 'click', () => showFlat(button.dataset.yardView === 'flat')));
  figure.querySelectorAll('[data-scene-action]').forEach(button => on(button, 'click', async () => {
    const action = button.dataset.sceneAction;
    if (action === 'focus') renderer.fit(selected);
    if (action === 'fit') renderer.fit();
    if (action === 'isolate') {
      isolated = !isolated; button.setAttribute('aria-pressed', String(isolated)); renderer.isolate(isolated ? selected : null);
    }
    if (action === 'orbit' || action === 'pan') {
      renderer.setMode(action);
      ['orbit','pan'].forEach(mode => figure.querySelector('[data-scene-action="' + mode + '"]').setAttribute('aria-pressed', String(mode === action)));
    }
    if (action === 'fullscreen') {
      try { if (document.fullscreenElement === figure) await document.exitFullscreen(); else await figure.requestFullscreen(); }
      catch (_) { error.textContent = '브라우저에서 전체 화면을 허용하지 않습니다.'; }
    }
  }));
  on(document, 'fullscreenchange', () => { renderer.resize(); figure.querySelector('[data-scene-action="fullscreen"]').setAttribute('aria-pressed', String(document.fullscreenElement === figure)); });
  on(preview, 'yardstagechange', event => selectFrame(Number(event.detail.index), event.detail.animate !== false));
  on(document, 'visibilitychange', () => { if (document.hidden) { stop(); if (!draft) selectFrame(index, false); } });
  on(animateMotion, 'change', () => { stop(); if (!draft) selectFrame(index, false); });
  on(reduced, 'change', () => { animateMotion.checked = !reduced.matches; stop(); if (!draft) selectFrame(index, false); });
  on(window, 'pagehide', event => { stop(); if (!draft) selectFrame(index, false); if (!event.persisted) destroy(); });
  on(window, 'pageshow', () => renderer.resize());
  on(canvas, 'webglcontextlost', event => { event.preventDefault(); destroy(); fallback(figure); });
  function destroy() {
    if (destroyed) return;
    destroyed = true; stop(); cancelAnimationFrame(raf); events.abort();
    controls.forEach(button => { button.disabled = false; }); renderer?.dispose();
  }
  play.hidden = scene.frames.length < 2 || controls.length !== scene.frames.length;
  if (scene.kind === 'map-definition') {
    figure.classList.add('map-definition');
    figure.querySelector('.model-inspector').hidden = true;
    editButton.disabled = true;
    figure.querySelector('[data-scene-step]').textContent = scene.frames[index].label;
    status(scene.frames[index].label);
  }
  editTools.hidden = scene.kind === 'map-definition';
  loadPlan.hidden = scene.frames.length < 2 || controls.length !== scene.frames.length;
  figure.querySelector('[data-scene-action="fullscreen"]').hidden = !document.fullscreenEnabled;
  preview.classList.add('yard-linked');
  paint(current); fillCargo(); syncLinked(); views.hidden = false; showFlat(false);
  canvas.dataset.moving = 'false'; figure.dataset.ready = 'true'; figure.dataset.renderer = 'webgl'; renderer.resize(); renderer.fit();
  return {destroy};
}

if (typeof document !== 'undefined') document.querySelectorAll('[data-yard-3d]').forEach(figure => {
  mountYardViewer(figure).catch(() => fallback(figure));
});

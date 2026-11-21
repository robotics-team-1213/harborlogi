/** Pure state helpers and DOM bindings for switching inline 3D scenes and move arrows. */

export function toggleUpcomingSelection(activeKey, clickedKey) {
  if (!clickedKey) return null;
  return activeKey === clickedKey ? null : clickedKey;
}

export function stageButtonSpecs(scene) {
  const frames = Array.isArray(scene?.frames) ? scene.frames : [];
  if (frames.length <= 1) return [];
  return frames.map((frame, index) => ({
    index,
    label: index === 0 ? '관측' : `${index} · ${frame.move?.phase_label || frame.phase || '예상'}`,
    pressed: index === 0,
  }));
}

export function arrowsFromScene(scene) {
  if (Array.isArray(scene?.arrows)) return scene.arrows;
  const frames = Array.isArray(scene?.frames) ? scene.frames : [];
  const arrows = [];
  for (const frame of frames) {
    const move = frame?.move;
    if (!move) continue;
    arrows.push({
      number: move.number ?? arrows.length + 1,
      cargo: move.cargo ?? move.cargo_id ?? '',
      phase: move.phase ?? move.phase_label ?? '',
      source: move.source_place ?? (move.source_level ? `${move.source} ${move.source_level}층` : move.source || ''),
      destination: move.destination_place ?? (move.destination_level ? `${move.destination} ${move.destination_level}층` : move.destination || ''),
      arm: move.arm ?? move.primary_arm ?? '',
    });
  }
  return arrows;
}

export function renderArrows(container, arrows, {title = '', emptyText = '예상 이동 경로가 없습니다.'} = {}) {
  if (!container) return;
  const items = Array.isArray(arrows) ? arrows : [];
  if (!items.length) {
    const empty = container.ownerDocument.createElement('p');
    empty.className = 'helptext yard-arrows-empty';
    empty.textContent = emptyText;
    container.replaceChildren(empty);
    return;
  }
  const doc = container.ownerDocument;
  const heading = doc.createElement('h3');
  heading.textContent = title ? `${title} · 이동 경로` : '이동 경로';
  const list = doc.createElement('ol');
  list.className = 'yard-arrows-list';
  for (const move of items) {
    const li = doc.createElement('li');
    const num = doc.createElement('span');
    num.className = 'yard-arrow-num';
    num.textContent = String(move.number ?? '');
    const badge = doc.createElement('span');
    badge.className = move.phase === '반출' ? 'badge READY' : 'badge';
    badge.textContent = move.phase || '';
    const cargo = doc.createElement('strong');
    cargo.textContent = move.cargo || '';
    const route = doc.createElement('span');
    route.className = 'yard-arrow-route';
    route.textContent = `${move.source || ''} → ${move.destination || ''}`;
    li.append(num, badge, cargo, route);
    if (move.arm) {
      const arm = doc.createElement('span');
      arm.className = 'yard-arrow-arm';
      arm.textContent = `팔 ${move.arm}`;
      li.append(arm);
    }
    list.append(li);
  }
  container.replaceChildren(heading, list);
}

export function bindUpcomingPlans(root, {getViewer, onSwitch} = {}) {
  const buttons = [...root.querySelectorAll('[data-upcoming-plan]')];
  if (!buttons.length) return {destroy() {}, activeKey: () => null};
  const doc = root.ownerDocument || root;
  const figure = root.querySelector('[data-yard-3d]');
  const preview = figure?.closest('[data-yard-preview]') || root.querySelector('[data-yard-preview]');
  const arrowsContainer = root.querySelector('[data-yard-arrows]');
  const baseSceneId = figure?.dataset?.scene || 'yard-scene-data';
  const baseArrowsSnapshot = arrowsContainer
    ? [...arrowsContainer.childNodes].map(node => node.cloneNode(true))
    : [];
  const baseArrowsHidden = arrowsContainer ? arrowsContainer.hidden : true;
  let activeKey = null;
  const controller = typeof AbortController === 'function' ? new AbortController() : null;

  const readScene = id => {
    const node = id ? doc.getElementById(id) : null;
    return node ? JSON.parse(node.textContent) : null;
  };

  const applyViewerScene = (targetKey, scene, autoplay) => {
    const viewer = getViewer?.();
    if (!viewer || !scene) return;
    if (typeof viewer.then === 'function') {
      viewer.then(resolved => {
        if (activeKey === targetKey && resolved?.setScene) {
          resolved.setScene(scene, {autoplay});
        }
      });
    } else if (viewer.setScene) {
      viewer.setScene(scene, {autoplay});
    }
  };

  for (const button of buttons) {
    const handler = () => {
      const clickedKey = button.dataset.upcomingPlan;
      const nextKey = toggleUpcomingSelection(activeKey, clickedKey);
      activeKey = nextKey;
      for (const candidate of buttons) {
        candidate.setAttribute('aria-pressed', String(candidate.dataset.upcomingPlan === activeKey));
      }
      if (activeKey) {
        const nextScene = readScene(button.dataset.upcomingScene);
        if (!nextScene) return;
        if (preview) preview.dataset.activePlan = activeKey;
        if (arrowsContainer) {
          arrowsContainer.hidden = false;
          renderArrows(arrowsContainer, arrowsFromScene(nextScene), {
            title: nextScene.title || button.querySelector('strong')?.textContent || '',
          });
        }
        applyViewerScene(activeKey, nextScene, true);
        onSwitch?.({activeKey, scene: nextScene, autoplay: true});
      } else {
        if (preview) delete preview.dataset.activePlan;
        if (arrowsContainer) {
          arrowsContainer.hidden = baseArrowsHidden;
          arrowsContainer.replaceChildren(...baseArrowsSnapshot.map(node => node.cloneNode(true)));
        }
        const baseScene = readScene(baseSceneId);
        applyViewerScene(null, baseScene, false);
        onSwitch?.({activeKey: null, scene: baseScene, autoplay: false});
      }
    };
    if (controller) button.addEventListener('click', handler, {signal: controller.signal});
    else button.addEventListener('click', handler);
  }

  return {
    destroy() { controller?.abort(); },
    activeKey: () => activeKey,
  };
}

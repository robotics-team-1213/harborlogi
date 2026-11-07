/** Logical selection shared by the two browser-local views. */
export function selectionFor(box) {
  if (!box) return null;
  const coveredPositions = [];
  for (let row = box.row; row < box.row + box.depth; row++)
    for (let column = box.column; column < box.column + box.width; column++)
      coveredPositions.push({area:box.area, row, column, level:box.level});
  return {cargoId:box.id, area:box.area, row:box.row, column:box.column,
    level:box.level, orientation:box.orientation, coveredPositions};
}

export function atPosition(boxes, position) {
  return boxes.find(box => box.area === position.area && box.level === position.level &&
    position.row >= box.row && position.row < box.row + box.depth &&
    position.column >= box.column && position.column < box.column + box.width);
}

export const OCCUPANCY_CAP = 3; // Same cap as yard_occupancy.STEP_CAP: step 3 means three or more levels.

/** Per-column stack height of one area: the highest level of any cargo covering the column (multi-cell cargo = one box). */
export function columnHeights(boxes, areaId) {
  const heights = new Map();
  for (const box of boxes) {
    if (box.area !== areaId) continue;
    for (let row = box.row; row < box.row + box.depth; row++)
      for (let column = box.column; column < box.column + box.width; column++)
        heights.set(`${row},${column}`, Math.max(heights.get(`${row},${column}`) || 0, box.level));
  }
  return heights;
}

export function occupancyFor(heights, row, column, depth = 1, width = 1) {
  let height = 0;
  for (let r = row; r < row + depth; r++)
    for (let c = column; c < column + width; c++) height = Math.max(height, heights.get(`${r},${c}`) || 0);
  return {height, step: Math.min(height, OCCUPANCY_CAP)};
}

/** A slot site's place as yard_rows.place_label names it: '1행 2칸 3층', '임시 적재(왼쪽) 2번', '컨베이어(반출)'. */
export function slotLabel(areas, item, slotCells) {
  if (!item) return '';
  const area = areas.find(a => a.id === item.area) || {id:item.area, label:item.area, rows:1, columns:slotCells, levels:1};
  if (area.id === 'yard') return `${item.row}행 ${Math.floor((item.column - 1) / slotCells) + 1}칸 ${item.level}층`;
  const lone = area.rows * Math.floor(area.columns / slotCells) === 1;
  return `${area.label}${lone ? '' : ` ${item.row}번`}${area.levels === 1 ? '' : ` ${item.level}층`}`;
}

/** Every slot of one area, row by row: the cargo on each level and the highest occupied level (KAN-115). */
export function slotStacks(boxes, area, slotCells) {
  const slots = [];
  for (let row = 1; row <= area.rows; row++) {
    for (let first = 1, number = 1; first <= area.columns; first += slotCells, number++) {
      const levels = [];
      for (let level = 1; level <= area.levels; level++) {
        const found = []; let gap = false;
        for (let column = first; column < first + slotCells && column <= area.columns; column++) {
          const box = atPosition(boxes, {area:area.id, row, column, level});
          if (!box) gap = true;
          else if (!found.includes(box)) found.push(box);
        }
        if (found.length) levels.push({level, boxes:found, partial:gap || found.length > 1});
      }
      slots.push({row, number, first, levels, top:levels.length ? levels[levels.length - 1].level : 0});
    }
  }
  return slots;
}

const SLOT_ROLES = {target:'target', blocker:'rehandle', rehandle:'rehandle'};

export function mountLinkedGrid(preview, scene, onSelect, inspectDestination = () => null) {
  const rehandle = new Set(scene.rehandle || []);  // Stored plan moves; absent on pages without a plan.
  const original = preview.querySelector('[data-yard-flat]');
  const grid = document.createElement('div');
  grid.dataset.yardLinkedFlat = ''; grid.className = 'linked-flat';
  original.after(grid);
  const levels = new Map(scene.areas.map(area => [area.id, 1]));
  const slotCells = scene.slot_cells || 0;  // fixed 1×3 slots: one card per slot with every level, no level tabs
  let selected = null, boxes = [], mode = '', stageLabel = '';
  const address = (area, row, column, level) => area.addresses?.[`${row},${column},${level}`] ||
    `${area.id} · ${row}행 ${column}열 ${level}층`;
  function render() {
    grid.replaceChildren();
    const heading = document.createElement('h3');
    heading.textContent = mode === 'map-definition' ? '맵 정의 · 배치 미관측' :
      mode === 'local-draft' ? '로컬 가상 배치 · 최종 배치 · 서버에 저장되지 않음' :
      `${mode === 'original' ? '원본 관측' : '계획 후 예상'} · ${slotCells ? '자리 평면' : '층별 평면'}`;
    grid.append(heading); grid.dataset.source = mode;
    const showOccupancy = mode !== 'map-definition';  // Nothing is observed there, so no height is claimed.
    if (showOccupancy && !slotCells) {
      const legend = document.createElement('ul'); legend.className = 'occupancy-legend'; legend.dataset.linkedOccupancyLegend = '';
      legend.setAttribute('aria-label', '점유 높이와 재취급 예정 범례');
      for (const [step, text] of [[0,'빈 칸'],[1,'1층'],[2,'2층'],[3,'3층 이상']]) {
        const item = document.createElement('li'), swatch = document.createElement('i');
        swatch.className = 'occ-swatch'; swatch.dataset.step = step; swatch.textContent = step; swatch.setAttribute('aria-hidden','true');
        item.append(swatch, text); legend.append(item);
      }
      if (rehandle.size) {
        const item = document.createElement('li'), swatch = document.createElement('i');
        swatch.className = 'occ-swatch'; swatch.dataset.rehandle = ''; swatch.textContent = '재'; swatch.setAttribute('aria-hidden','true');
        item.append(swatch, '재취급 예정 화물'); legend.append(item);
      }
      const basis = document.createElement('p'); basis.className = 'helptext'; basis.dataset.linkedOccupancyBasis = '';
      const kind = mode === 'local-draft' ? '가상 배치 · 실적 아님' : /예상/.test(stageLabel || '') ? '예상 · 실적 아님' : '관측값';
      basis.textContent = `쌓인 높이: ${mode === 'local-draft' ? '로컬 가상 배치' : stageLabel || '관측'} 기준 칸별 층 수 · ${kind} · 재취급 예정은 저장된 계획 기준`;
      grid.append(legend, basis);
    }
    if (mode === 'local-draft') {
      const bar = document.createElement('div'); bar.className = 'linked-edit-bar';
      const label = document.createElement('label'); label.textContent = '이동할 화물';
      const picker = document.createElement('select'); picker.dataset.linkedCargo = '';
      picker.add(new Option('화물 선택',''));
      boxes.forEach(box=>picker.add(new Option(slotCells ? box.id : `${box.id} · ${box.size}`,box.id)));
      picker.value = selected?.cargoId || '';
      picker.addEventListener('change',()=>{
        const box = boxes.find(box=>box.id===picker.value);
        if (!box) { picker.value = selected?.cargoId || ''; return; }
        onSelect(selectionFor(box));
        grid.querySelector('[data-linked-cargo]')?.focus({preventScroll:true});
      });
      const feedback = document.createElement('p'); feedback.dataset.linkedFeedback = '';
      feedback.setAttribute('role','status');
      feedback.textContent = selected?.cargoId ? `${selected.cargoId} · 목적 ${slotCells ? '자리의 ‘놓기’를' : '칸을'} 누르면 이동합니다. ‘${slotCells ? '놓을 수 없음' : '이동 불가'}’은 이유를 확인합니다.` : '먼저 이동할 화물을 선택하세요.';
      label.append(picker); bar.append(label,feedback); grid.append(bar);
    }
    if (slotCells) { renderSlots(); return; }
    for (const area of scene.areas) {
      const section = document.createElement('section'); section.dataset.gridArea = area.id;
      const title = document.createElement('h4'); title.textContent = `${area.label} · ${area.rows}행 × ${area.columns}열 × ${area.levels}층`;
      const controls = document.createElement('div'); controls.className = 'layer-controls';
      for (let level = 1; level <= area.levels; level++) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'quiet';
        button.textContent = `${level}층${level === 1 ? ' · 바닥층' : ''}`;
        button.dataset.linkedLevel = level; button.setAttribute('aria-pressed', String(levels.get(area.id) === level));
        button.addEventListener('click', () => { levels.set(area.id, level); render();
          grid.querySelector(`[data-grid-area="${CSS.escape(area.id)}"] [data-linked-level="${level}"]`).focus(); });
        controls.append(button);
      }
      const heights = showOccupancy ? columnHeights(boxes, area.id) : null;
      const table = document.createElement('table'); table.className = 'yard-plane';
      const caption = table.createCaption(); caption.textContent = `${levels.get(area.id)}층 · 1행 위쪽 / 1열 왼쪽`;
      const head = table.createTHead().insertRow(); head.append(document.createElement('th'));
      for (let column = 1; column <= area.columns; column++) {
        const th = document.createElement('th'); th.scope = 'col'; th.textContent = `${column}열`; head.append(th);
      }
      const body = table.createTBody();
      for (let row = 1; row <= area.rows; row++) {
        const tr = body.insertRow(), th = document.createElement('th'); th.scope = 'row'; th.textContent = `${row}행`; tr.append(th);
        for (let column = 1; column <= area.columns; column++) {
          const position = {area:area.id, row, column, level:levels.get(area.id)};
          const box = atPosition(boxes, position);
          if (box && (box.row !== row || box.column !== column)) continue;
          const td = tr.insertCell(); td.className = `grid-cell ${box?.role || 'vacant'}`;
          td.colSpan = box?.width || 1; td.rowSpan = box?.depth || 1;
          const button = document.createElement('button'); button.type = 'button'; button.className = 'linked-cell';
          const identity = box ? selectionFor(box) : {...position, cargoId:null, orientation:null, coveredPositions:[position]};
          button.dataset.cargoId = box?.id || ''; button.dataset.selection = JSON.stringify(identity);
          button.dataset.destination = `${row},${column}`;
          const hint = !box ? inspectDestination(position) : null;
          if (hint) {
            button.dataset.dropState = hint.valid ? 'valid' : 'invalid';
            button.title = hint.valid ? '선택 화물을 이 칸으로 이동' : hint.reason;
          }
          const active = selected && (selected.cargoId ? selected.cargoId === box?.id : !box &&
            ['area','row','column','level'].every(key => position[key] === selected[key]));
          button.setAttribute('aria-pressed', String(Boolean(active)));
          const label = document.createElement('strong');
          label.textContent = box ? `${box.id} · ${box.size} · ${box.orientation}도` : hint ? hint.valid ? '여기로 이동' : '이동 불가' : mode === 'map-definition' ? '미관측 위치' : '빈 위치';
          const coordinates = document.createElement('span');
          coordinates.textContent = identity.coveredPositions.map(p => address(area, p.row, p.column, p.level)).join(' / ');
          button.append(label, coordinates);
          if (heights) {
            const {height, step} = occupancyFor(heights, row, column, box?.depth, box?.width);
            const planned = box && mode !== 'local-draft' && rehandle.has(box.id);
            const chip = document.createElement('span'); chip.className = 'linked-occupancy'; chip.dataset.linkedOccupancy = '';
            chip.dataset.step = step; chip.dataset.height = height;
            const spoken = document.createElement('span'); spoken.className = 'sr-only'; spoken.textContent = '쌓인 높이 ';
            chip.append(spoken, `${height}층${planned ? ' · 재취급 예정' : ''}`);  // the legend names it on screen
            td.dataset.step = step; td.dataset.height = height;
            if (planned) { button.dataset.rehandle = box.id; td.dataset.rehandle = box.id; }
            button.append(chip);
          }
          button.addEventListener('click', () => pick(identity)); td.append(button);
        }
      }
      const wrap = document.createElement('div'); wrap.className = 'linked-plane-scroll'; wrap.append(table);
      section.append(title, controls, wrap); grid.append(section);
    }
  }
  function pick(identity) {
    onSelect(identity);
    const target = [...grid.querySelectorAll('[data-selection]')].find(node => {
      const value = JSON.parse(node.dataset.selection);
      return identity.cargoId ? value.cargoId === identity.cargoId :
        ['area','row','column','level'].every(key => value[key] === identity[key]);
    });
    target?.focus({preventScroll:true});
  }
  // A slot site (KAN-115): the same site map as yard_rows.html. Trays sit beside the yard as presented,
  // each slot lists its levels, and a local draft drops the chosen cargo on top of a slot.
  function renderSlots() {
    const basis = document.createElement('p'); basis.className = 'helptext'; basis.dataset.linkedOccupancyBasis = '';
    basis.textContent = mode === 'map-definition' ? '자리 정의만 있습니다 · 화물 위치는 확인되지 않았습니다' :
      `자리마다 아래층부터 컨테이너 번호 · ${mode === 'local-draft' ? '가상 배치 · 실적 아님' : /예상/.test(stageLabel || '') ? '예상 · 실적 아님' : '관측값'}${rehandle.size && mode !== 'local-draft' ? ' · 재취급 예정은 저장된 계획 기준' : ''}`;
    const yard = scene.areas.find(area => area.id === 'yard') || scene.areas[0];
    const sides = {left:[], center:[], right:[]};
    for (const area of scene.areas)
      sides[area === yard ? 'center' : area.y + area.rows <= yard.y ? 'left' : area.y >= yard.y + yard.rows ? 'right' : 'center'].push(slotArea(area, area === yard));
    const frame = document.createElement('div'); frame.className = 'site-frame';
    const map = document.createElement('div'); map.className = 'site-map'; map.dataset.yardPlan = '';
    for (const [side, items] of Object.entries(sides)) {
      if (!items.length) continue;
      const column = document.createElement('div'); column.className = side === 'center' ? 'site-center' : `site-side site-${side}`;
      column.append(...items); map.append(column);
    }
    frame.append(map); grid.append(basis, frame);
  }
  function slotArea(area, isYard) {
    const section = document.createElement('section'); section.className = isYard ? 'site-yard' : 'site-tray';
    section.dataset.gridArea = area.id; section.setAttribute('aria-label', area.label);
    const slots = slotStacks(boxes, area, slotCells), perRow = slots.length / area.rows;
    const holder = document.createElement('div'); holder.className = isYard ? `yard-lanes lanes-${area.rows}` : 'tray-slots';
    for (let row = 1; row <= area.rows; row++) {
      const lane = isYard ? document.createElement('div') : holder;
      if (isYard) {
        const label = document.createElement('span'); label.className = 'lane-label'; label.textContent = `${row}행`;
        lane.className = 'yard-lane'; lane.append(label); holder.append(lane);
      }
      for (const slot of slots.filter(item => item.row === row))
        lane.append(slotCard(area, slot, isYard ? (perRow > 1 ? `${slot.number}칸` : '') : (area.rows > 1 ? `${row}번` : '')));
    }
    const title = document.createElement('h4'); title.textContent = area.label;
    const count = document.createElement('small');
    count.textContent = `${slots.reduce((sum, slot) => sum + slot.levels.length, 0)} / ${slots.length * area.levels}자리`;
    title.append(count); section.append(title, holder); return section;
  }
  function slotCard(area, slot, name) {
    const card = document.createElement('div'); card.className = `slot-card${slot.levels.length ? '' : ' empty'}`;
    card.dataset.yardRow = `${area.id}-R${slot.row}S${slot.number}`;
    if (name) { const label = document.createElement('span'); label.className = 'slot-name'; label.textContent = name; card.append(label); }
    for (const {level, boxes:found, partial} of slot.levels)
      for (const box of found) card.append(levelButton(area, level, box, partial));
    if (!slot.levels.length) {
      const empty = document.createElement('span'); empty.className = 'slot-empty';
      empty.textContent = mode === 'map-definition' ? '미관측' : '비어 있음'; card.append(empty);
    }
    const holdsSelected = slot.levels.some(entry => entry.boxes.some(box => box.id === selected?.cargoId));
    if (mode === 'local-draft' && selected?.cargoId && !holdsSelected && slot.top < area.levels) card.append(dropButton(area, slot, slot.top + 1));
    return card;
  }
  function levelButton(area, level, box, partial) {
    const planned = mode !== 'local-draft' && rehandle.has(box.id);
    const role = SLOT_ROLES[box.role] || (planned ? 'rehandle' : 'cargo');
    const identity = selectionFor(box);
    const button = document.createElement('button'); button.type = 'button'; button.className = `slot-level role-${role}`;
    button.dataset.cargoId = box.id; button.dataset.selection = JSON.stringify(identity);
    button.setAttribute('aria-pressed', String(selected?.cargoId === box.id));
    if (area.levels > 1) { const tier = document.createElement('b'); tier.textContent = `${level}층`; button.append(tier, ' '); }
    button.append(box.id);
    for (const [shown, text] of [[role === 'target', '반출 대상'], [role === 'rehandle', '재취급'], [partial, '일부 칸']]) {
      if (!shown) continue;
      const note = document.createElement('small'); note.textContent = text; button.append(note);
    }
    if (planned) button.dataset.rehandle = box.id;
    button.addEventListener('click', () => pick(identity));
    return button;
  }
  function dropButton(area, slot, level) {
    const position = {area:area.id, row:slot.row, column:slot.first, level};
    const identity = {...position, cargoId:null, orientation:null, coveredPositions:[position]};
    const hint = inspectDestination(position);
    const button = document.createElement('button'); button.type = 'button'; button.className = 'quiet slot-drop';
    button.dataset.selection = JSON.stringify(identity); button.dataset.destination = `${slot.row},${slot.first}`;
    if (hint) {
      button.dataset.dropState = hint.valid ? 'valid' : 'invalid';
      button.title = hint.valid ? '선택 화물을 이 자리 맨 위로 이동' : hint.reason;
    }
    button.textContent = hint && !hint.valid ? '놓을 수 없음' : area.levels > 1 ? `${level}층에 놓기` : '여기에 놓기';
    button.addEventListener('click', () => pick(identity));
    return button;
  }
  return {
    grid,
    feedback(text) { const node = grid.querySelector('[data-linked-feedback]'); if (node) node.textContent = text; },
    update(values, nextMode, value, label = '') {
      boxes = values; mode = nextMode; selected = value; stageLabel = label; original.hidden = true;
      if (value) levels.set(value.area, value.level);
      preview.dataset.selection = JSON.stringify(value); render();
    },
  };
}

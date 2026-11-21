import assert from 'node:assert/strict';
import {
  arrowsFromScene,
  bindUpcomingPlans,
  renderArrows,
  stageButtonSpecs,
  toggleUpcomingSelection,
} from '../harbor_web/static/harbor_web/yard-inline.mjs';

// 1. Toggle logic: inactive activates, active restores null.
assert.equal(toggleUpcomingSelection(null, 'plan-1'), 'plan-1');
assert.equal(toggleUpcomingSelection('plan-1', 'plan-1'), null);
assert.equal(toggleUpcomingSelection('plan-1', 'plan-2'), 'plan-2');

// 2. Stage button specs for multi-frame and single-frame scenes.
assert.deepEqual(stageButtonSpecs({frames: [{index: 0, label: '관측'}]}), []);
assert.deepEqual(stageButtonSpecs({
  frames: [
    {index: 0, label: '관측한 배치', move: null},
    {index: 1, label: '1단계', move: {phase_label: '재취급'}},
    {index: 2, label: '2단계', move: {phase_label: '반출'}},
  ],
}), [
  {index: 0, label: '관측', pressed: true},
  {index: 1, label: '1 · 재취급', pressed: false},
  {index: 2, label: '2 · 반출', pressed: false},
]);

// 3. Arrow extraction supports explicit scene.arrows and frame.move fallback.
const explicit = [{number: 1, cargo: 'K2', phase: '재취급', source: '1행 1열 2층', destination: '임시 판 A 1레인', arm: 'A'}];
assert.deepEqual(arrowsFromScene({arrows: explicit}), explicit);
assert.deepEqual(arrowsFromScene({
  frames: [
    {index: 0, move: null},
    {index: 1, move: {number: 1, cargo_id: 'K1', phase_label: '반출', source_place: '1행 1열 1층', destination_place: '반출대', primary_arm: 'A'}},
  ],
}), [{number: 1, cargo: 'K1', phase: '반출', source: '1행 1열 1층', destination: '반출대', arm: 'A'}]);

// 4. Minimal DOM test harness for renderArrows & bindUpcomingPlans (no external deps).
class FakeElement {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase();
    this.ownerDocument = doc;
    this.className = '';
    this.dataset = {};
    this.attributes = {};
    this.childNodes = [];
    this.listeners = new Map();
    this.hidden = false;
    this.textContent = '';
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  click() {
    for (const fn of this.listeners.get('click') || []) fn({currentTarget: this, target: this});
  }
  append(...nodes) {
    for (const node of nodes) this.childNodes.push(node);
  }
  replaceChildren(...nodes) {
    this.childNodes = [...nodes];
  }
  cloneNode(deep = false) {
    const copy = new FakeElement(this.tagName, this.ownerDocument);
    copy.className = this.className;
    copy.dataset = {...this.dataset};
    copy.attributes = {...this.attributes};
    copy.hidden = this.hidden;
    copy.textContent = this.textContent;
    if (deep) copy.childNodes = this.childNodes.map(child => child.cloneNode(true));
    return copy;
  }
  closest(selector) {
    if (selector === '[data-yard-preview]') return this.attributes['data-yard-preview'] !== undefined ? this : this._parent?.closest(selector) || null;
    return null;
  }
  querySelector(selector) {
    if (selector === 'strong' && this.tagName === 'STRONG') return this;
    for (const child of this.childNodes) {
      if (selector === 'strong' && child.tagName === 'STRONG') return child;
      const found = child.querySelector?.(selector);
      if (found) return found;
    }
    return null;
  }
}

const fakeDoc = {
  byId: new Map(),
  nodes: [],
  createElement(tag) { return new FakeElement(tag, this); },
  getElementById(id) { return this.byId.get(id) || null; },
  querySelector(sel) { return this.nodes.find(n => n._match === sel) || null; },
  querySelectorAll(sel) { return this.nodes.filter(n => n._match === sel); },
};

const preview = new FakeElement('div', fakeDoc);
preview._match = '[data-yard-preview]';
preview.attributes['data-yard-preview'] = '';

const figure = new FakeElement('section', fakeDoc);
figure._match = '[data-yard-3d]';
figure.dataset.scene = 'yard-scene-data';
figure._parent = preview;

const arrowsBox = new FakeElement('section', fakeDoc);
arrowsBox._match = '[data-yard-arrows]';
const baseEmpty = new FakeElement('p', fakeDoc);
baseEmpty.textContent = '기본 안내';
arrowsBox.append(baseEmpty);

const btn1 = new FakeElement('button', fakeDoc);
btn1._match = '[data-upcoming-plan]';
btn1.dataset.upcomingPlan = 'plan-1';
btn1.dataset.upcomingScene = 'upcoming-scene-1';
btn1.setAttribute('aria-pressed', 'false');

const btn2 = new FakeElement('button', fakeDoc);
btn2._match = '[data-upcoming-plan]';
btn2.dataset.upcomingPlan = 'plan-2';
btn2.dataset.upcomingScene = 'upcoming-scene-2';
btn2.setAttribute('aria-pressed', 'false');

fakeDoc.nodes.push(preview, figure, arrowsBox, btn1, btn2);

const baseScene = {title: '기준', areas: [{id: 'yard', rows: 3, columns: 6, levels: 3}], frames: [{index: 0, label: '관측한 배치', boxes: []}]};
const plan1Scene = {
  title: 'K1 반출',
  areas: baseScene.areas,
  frames: [{index: 0, boxes: []}, {index: 1, boxes: []}],
  arrows: [{number: 1, cargo: 'K1', phase: '반출', source: '1행 1열 1층', destination: '반출대', arm: 'A'}],
};
const plan2Scene = {
  title: 'K3 반출',
  areas: baseScene.areas,
  frames: [{index: 0, boxes: []}, {index: 1, boxes: []}],
  arrows: [{number: 1, cargo: 'K3', phase: '반출', source: '2행 1열 1층', destination: '반출대', arm: 'A'}],
};
fakeDoc.byId.set('yard-scene-data', {textContent: JSON.stringify(baseScene)});
fakeDoc.byId.set('upcoming-scene-1', {textContent: JSON.stringify(plan1Scene)});
fakeDoc.byId.set('upcoming-scene-2', {textContent: JSON.stringify(plan2Scene)});

const calls = [];
const mockViewer = {
  setScene(scene, opts) { calls.push({title: scene.title, autoplay: opts.autoplay}); },
};

const controller = bindUpcomingPlans(fakeDoc, {getViewer: () => mockViewer});
// Click inactive plan-1 -> activates, switches viewer with autoplay: true, updates arrows.
btn1.click();
assert.equal(btn1.getAttribute('aria-pressed'), 'true');
assert.equal(btn2.getAttribute('aria-pressed'), 'false');
assert.equal(controller.activeKey(), 'plan-1');
assert.equal(preview.dataset.activePlan, 'plan-1');
assert.deepEqual(calls.at(-1), {title: 'K1 반출', autoplay: true});
assert.equal(arrowsBox.childNodes[0].textContent, 'K1 반출 · 이동 경로');
assert.equal(arrowsBox.childNodes[1].childNodes.length, 1);

// Click plan-2 -> switches active button, switches viewer, updates arrows.
btn2.click();
assert.equal(btn1.getAttribute('aria-pressed'), 'false');
assert.equal(btn2.getAttribute('aria-pressed'), 'true');
assert.equal(controller.activeKey(), 'plan-2');
assert.deepEqual(calls.at(-1), {title: 'K3 반출', autoplay: true});
assert.equal(arrowsBox.childNodes[0].textContent, 'K3 반출 · 이동 경로');

// Click active plan-2 again -> closes selection and restores base scene & arrows.
btn2.click();
assert.equal(btn2.getAttribute('aria-pressed'), 'false');
assert.equal(controller.activeKey(), null);
assert.equal(preview.dataset.activePlan, undefined);
assert.deepEqual(calls.at(-1), {title: '기준', autoplay: false});
assert.equal(arrowsBox.childNodes[0].textContent, '기본 안내');

// renderArrows empty state clears cleanly.
renderArrows(arrowsBox, [], {emptyText: '비어 있음'});
assert.equal(arrowsBox.childNodes[0].textContent, '비어 있음');

console.log('Yard inline 3D scene switching, arrow updates and toggle-restore passed');

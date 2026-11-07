import assert from 'node:assert/strict';
import {selectionFor, atPosition, slotLabel, slotStacks} from '../harbor_web/static/harbor_web/yard-selection.mjs';

const boxes = [{id:'wide', area:'yard', row:2, column:1, level:3, orientation:0, width:3, depth:1},
  {id:'rotated', area:'temp', row:1, column:2, level:1, orientation:90, width:1, depth:2}];
const original = structuredClone(boxes);
assert.equal(atPosition(boxes, {area:'yard', row:2, column:3, level:3}).id, 'wide');
assert.equal(atPosition(boxes, {area:'yard', row:2, column:3, level:2}), undefined);
assert.equal(atPosition(boxes, {area:'temp', row:2, column:2, level:1}).id, 'rotated');
assert.equal(atPosition(boxes, {area:'yard', row:1, column:2, level:1}), undefined);
const selection = selectionFor(boxes[1]);
assert.deepEqual(selection, {cargoId:'rotated', area:'temp', row:1, column:2, level:1, orientation:90,
  coveredPositions:[{area:'temp', row:1, column:2, level:1}, {area:'temp', row:2, column:2, level:1}]});
selection.coveredPositions[0].row = 99;
assert.deepEqual(boxes, original);
assert.equal(selectionFor(null), null);
console.log('Logical linked selection: covered cells, levels, areas, rotations and source immutability passed');

// A slot site (KAN-115): 1×3 slots, every level of a slot at once, and place names like yard_rows.place_label.
const areas = [{id:'yard', label:'장치 야드(배)', rows:3, columns:6, levels:3},
  {id:'temp_l', label:'임시 적재(왼쪽)', rows:3, columns:3, levels:1}, {id:'stand', label:'컨베이어(반출)', rows:1, columns:3, levels:1}];
const site = [{id:'K1', area:'yard', row:1, column:4, level:1, width:3, depth:1}, {id:'K2', area:'yard', row:1, column:4, level:2, width:3, depth:1},
  {id:'narrow', area:'yard', row:2, column:1, level:1, width:2, depth:1}, {id:'T1', area:'temp_l', row:2, column:1, level:1, width:3, depth:1}];
const yardSlots = slotStacks(site, areas[0], 3);
assert.equal(yardSlots.length, 6);
const second = yardSlots.find(slot => slot.row === 1 && slot.number === 2);
assert.deepEqual([second.first, second.top, second.levels.map(entry => [entry.level, entry.boxes.map(box => box.id), entry.partial])],
  [4, 2, [[1, ['K1'], false], [2, ['K2'], false]]]);
assert.deepEqual(yardSlots.find(slot => slot.row === 2 && slot.number === 1).levels.map(entry => entry.partial), [true]);
assert.deepEqual(yardSlots.find(slot => slot.row === 3 && slot.number === 1), {row:3, number:1, first:1, levels:[], top:0});
assert.deepEqual(slotStacks(site, areas[1], 3).map(slot => slot.levels.length), [0, 1, 0]);
assert.equal(slotLabel(areas, {area:'yard', row:1, column:4, level:2}, 3), '1행 2칸 2층');
assert.equal(slotLabel(areas, {area:'temp_l', row:2, column:1, level:1}, 3), '임시 적재(왼쪽) 2번');
assert.equal(slotLabel(areas, {area:'stand', row:1, column:1, level:1}, 3), '컨베이어(반출)');
assert.equal(slotLabel(areas, null, 3), '');
console.log('Slot site: slots per row, levels at once, partial slots and place names passed');

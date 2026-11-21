import assert from 'node:assert/strict';
import {applyReasonText, assignTiers, buildLaneStrings, frontIndex, moveItem} from '../harbor_web/static/harbor_web/reorder-model.mjs';

const sample = [
  {id: '10', version: 2, tier: 1, code: 'C9', running: true},
  {id: '11', version: 1, tier: 2, code: 'C5', running: false},
  {id: '12', version: 3, tier: 3, code: 'C2', running: false},
];

assert.equal(frontIndex(sample), 1);
assert.deepEqual(moveItem(sample, '12', frontIndex(sample)).map(x => x.code), ['C9', 'C2', 'C5']);
assert.deepEqual(moveItem(sample, '10', 2).map(x => x.code), ['C9', 'C5', 'C2']);  // running cannot move
assert.deepEqual(moveItem(sample, '12', 0).map(x => x.code), ['C9', 'C2', 'C5']);  // clamped after running

const reordered = moveItem(sample, '12', frontIndex(sample));
assert.deepEqual(assignTiers(reordered).map(x => [x.code, x.tier]), [['C9', 1], ['C2', 3], ['C5', 3]]);
assert.deepEqual(buildLaneStrings(reordered), ['FIXED|10:2', 'FIXED|', 'FIXED|12:3,11:1']);

assert.equal(applyReasonText({ok: true}), '순서 검증 통과 · 실행은 ROSY Fleet에서 승인한 뒤 시작됩니다');
assert.equal(applyReasonText({ok: false, reasons: ['실행 중인 반출 작업보다 앞에 둘 수 없습니다.']}),
  '적용 불가: 실행 중인 반출 작업보다 앞에 둘 수 없습니다.');
console.log('Reorder model: front index, running guard, monotonic lanes and reason formatting passed');

import assert from 'node:assert/strict';
import {columnHeights, occupancyFor, OCCUPANCY_CAP} from '../harbor_web/static/harbor_web/yard-selection.mjs';

const box = (id, area, row, column, level, width = 1, depth = 1) => ({id, area, row, column, level, width, depth});
const observed = [box('C1','yard',1,1,1), box('C2','yard',1,1,2), box('W','yard',2,1,1,3), box('T','temp',1,1,1)];
const heights = columnHeights(observed, 'yard');
assert.equal(occupancyFor(heights, 1, 1).height, 2);
assert.equal(occupancyFor(heights, 1, 2).height, 0);
assert.deepEqual(['2,1','2,2','2,3'].map(k => heights.get(k)), [1, 1, 1]);  // multi-cell cargo is one box on every covered column
assert.equal(occupancyFor(heights, 2, 1, 1, 3).height, 1);
assert.equal(columnHeights(observed, 'temp').size, 1);
// A stage after C2 moves onto W: heights follow the stage boxes, not the observation.
const after = observed.map(b => b.id === 'C2' ? box('C2','yard',2,2,2) : b);
const next = columnHeights(after, 'yard');
assert.equal(next.get('1,1'), 1);
assert.equal(next.get('2,2'), 2);
const tall = [1,2,3,4,5].map(level => box(`H${level}`,'yard',1,1,level));
assert.deepEqual(occupancyFor(columnHeights(tall, 'yard'), 1, 1), {height:5, step:OCCUPANCY_CAP});
console.log('Linked occupancy: per-column heights per stage, multi-cell as one box, cap at three passed');

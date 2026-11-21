/** Pure reorder/validate helpers shared by static/harbor_web/reorder.js and tests/reorder.test.mjs. */

export function frontIndex(items) {
  let lastRunning = -1;
  items.forEach((item, index) => {
    if (item.running) lastRunning = index;
  });
  return lastRunning + 1;
}

export function moveItem(items, id, targetIndex) {
  const index = items.findIndex(item => item.id === id);
  if (index === -1 || items[index].running) return items.slice();
  const minIndex = frontIndex(items);
  const clamped = Math.max(minIndex, Math.min(targetIndex, items.length - 1));
  const next = items.slice();
  const [item] = next.splice(index, 1);
  next.splice(clamped, 0, item);
  return next;
}

/** Keep tier numbers monotonic (1 <= tier <= 3) so outbound_reorder preserves the exact flat sequence. */
export function assignTiers(items) {
  let floor = 1;
  return items.map(item => {
    const tier = Math.max(floor, Math.min(3, item.tier || 1));
    floor = tier;
    return {...item, tier};
  });
}

export function buildLaneStrings(items, fixedFlags = [true, true, true]) {
  const buckets = [[], [], []];
  for (const item of assignTiers(items)) {
    buckets[item.tier - 1].push(`${item.id}:${item.version}`);
  }
  return buckets.map((entries, index) => `${fixedFlags[index] ? 'FIXED' : 'ANY'}|${entries.join(',')}`);
}

export function applyReasonText(validation) {
  if (!validation) return '';
  if (validation.ok) return '순서 검증 통과 · 실행은 ROSY Fleet에서 승인한 뒤 시작됩니다';
  const first = Array.isArray(validation.reasons) && validation.reasons.length
    ? validation.reasons[0]
    : '이 순서로는 반출할 수 없습니다.';
  return `적용 불가: ${first}`;
}

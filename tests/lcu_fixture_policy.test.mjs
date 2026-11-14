import test from 'node:test';
import assert from 'node:assert/strict';
import { actionCode } from '../scripts/lcu_fixture_policy.mjs';

const observation = { content: [{ type: 'text', text: [
  '1 entry (settable) Full name',
  '2 check box (unchecked) I confirm this is synthetic test data',
  '3 push button Submit synthetic application',
  '4 entry Address and search bar',
  '5 text Full name',
].join('\n') }] };
test('fill is generated only from current editable indices and approved values', () => {
  const code = actionCode({ action: 'fill', index: 1, value: 'Ada Example' }, observation, ['Ada Example']);
  assert.match(code, /app\.click\(1\)/);
  assert.match(code, /typeText\("Ada Example"\)/);
  assert.match(code, /getAXState/);
});
test('checkboxes use click, not fill', () => {
  assert.match(actionCode({ action: 'click', index: 2 }, observation, []), /app\.click\(2\)/);
  assert.throws(() => actionCode({ action: 'fill', index: 2, value: 'Ada' }, observation, ['Ada']), /editable/);
});
test('stale indices, invented values, arbitrary actions and browser chrome are rejected', () => {
  for (const action of [
    { action: 'click', index: 999 }, { action: 'click', index: '1' },
    { action: 'fill', index: 1, value: 'Invented' }, { action: 'click', index: 4 },
    { action: 'fill', index: 5, value: 'Ada' }, { action: 'js', code: 'process.exit()' },
  ]) assert.throws(() => actionCode(action, observation, ['Ada']));
});
test('synthetic submit is permitted and observed afterward', () => {
  assert.match(actionCode({ action: 'click', index: 3 }, observation, []), /getAXState/);
});
test('approved text is JSON-escaped, never interpolated as executable code', () => {
  const value = 'name"; process.exit(); //';
  const code = actionCode({ action: 'fill', index: 1, value }, observation, [value]);
  assert.ok(code.includes(`app.typeText(${JSON.stringify(value)})`));
});

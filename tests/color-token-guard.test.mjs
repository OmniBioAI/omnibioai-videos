import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

test('portal CSS contains no literal colors outside its one marked token mapping block', () => {
  const css = fs.readFileSync('portal/portal.css', 'utf8');
  const blocks = css.match(/\/\* color-token-mapping:start[\s\S]*?\/\* color-token-mapping:end \*\//g) || [];
  assert.equal(blocks.length, 1);
  const outside = css.replace(blocks[0], '');
  const explicitExceptions = [];
  const literals = outside.match(/#[\da-f]{3,8}\b|\brgba?\s*\([^)]*\)/gi) || [];
  assert.deepEqual(literals, explicitExceptions);
});

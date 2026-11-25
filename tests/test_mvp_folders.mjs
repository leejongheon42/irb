import assert from 'node:assert/strict';
import test from 'node:test';
import {droppedFiles} from '../irb_automation/web/static/mvp-folders.mjs';

const fileEntry = name => ({name, isFile: true, file: resolve => resolve({name})});
const directory = (name, ...batches) => ({
  name, isDirectory: true,
  createReader() {
    let index = 0;
    return {readEntries: resolve => resolve(batches[index++] || [])};
  },
});

test('a mixed drop reads all directory batches and preserves nested paths', async () => {
  const firstBatch = Array.from({length: 100}, (_, i) => fileEntry(`${i}.pdf`));
  const nested = directory('nested', [fileEntry('0.pdf')]);
  const root = directory('docs', firstBatch, [nested, directory('empty')]);
  let active = true;
  const items = [root, fileEntry('extra.pdf')].map(entry => ({
    kind: 'file', getAsFile: () => null,
    webkitGetAsEntry: () => { assert.ok(active); return entry; },
  }));
  const files = droppedFiles({items});
  active = false;
  const paths = [];
  for await (const item of files) paths.push(item.path || item.file.name);
  assert.deepEqual(paths, [...firstBatch.map(file => `docs/${file.name}`), 'docs/nested/0.pdf', 'extra.pdf']);
});

test('an unreadable entry reports its path and the next file is still uploaded', async () => {
  const root = directory('docs', [{name: 'broken', isFile: true, file: (_, reject) => reject()}, fileEntry('ok.pdf')]);
  const items = [{kind: 'file', webkitGetAsEntry: () => root, getAsFile: () => null}];
  const results = [];
  for await (const item of droppedFiles({items})) results.push(item);
  assert.ok(results[0].error);
  assert.equal(results[0].path, 'docs/broken');
  assert.equal(results[1].path, 'docs/ok.pdf');
});

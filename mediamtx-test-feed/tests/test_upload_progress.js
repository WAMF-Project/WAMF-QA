// Run with: node --test tests/test_upload_progress.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function setup(fetchOverride) {
  const elements = {};
  const requests = [];
  class XHR {
    constructor() { this.upload = {}; requests.push(this); }
    open(method, url) { assert.equal(method, 'POST'); assert.equal(url, '/api/upload'); }
    send(data) { this.data = data; }
  }
  const context = {
    document: {querySelector(selector) {
      return elements[selector] ||= {
        value: '', disabled: false, hidden: true, files: ['video'],
        options: [],
        replaceChildren() { this.options = []; }, add(option) { this.options.push(option); },
      };
    }},
    XMLHttpRequest: XHR,
    FormData: class { append() {} },
    Option: function (text, value) { this.text = text; this.value = value; },
    confirm: () => true,
    setTimeout() {},
    fetch: fetchOverride || (async () => ({ok: true, json: async () => ({files: ['uploaded.mp4'], state: 'stopped'})})),
  };
  const html = fs.readFileSync(path.join(__dirname, '../app/templates/index.html'), 'utf8');
  vm.runInNewContext(html.split('<script>')[1].split('</script>')[0], context);
  await new Promise(resolve => setImmediate(resolve));
  const submit = () => elements['#upload-form'].onsubmit({preventDefault() {}});
  return {elements, requests, submit, context};
}

test('measured progress, successful refresh/selection, and new-file reset', async () => {
  const {elements: e, requests, submit} = await setup();
  const done = submit();
  const xhr = requests[0];
  assert.equal(e['#upload'].disabled, true);
  assert.equal(e['#upload-progress-row'].hidden, false);
  assert.equal(e['#upload-percent'].textContent, '0%');
  xhr.upload.onprogress({lengthComputable: true, loaded: 37, total: 100});
  assert.equal(e['#upload-progress'].value, 37);
  assert.equal(e['#upload-percent'].textContent, '37%');
  xhr.upload.onprogress({lengthComputable: false, loaded: 90, total: 0});
  assert.equal(e['#upload-progress'].value, 37);
  xhr.status = 201; xhr.response = {filename: 'uploaded.mp4'}; xhr.onload();
  await done;
  assert.equal(e['#upload-progress'].value, 100);
  assert.equal(e['#video'].value, 'uploaded.mp4');
  assert.equal(e['#upload-status'].textContent, 'Uploaded uploaded.mp4.');
  assert.equal(e['#upload'].disabled, false);
  assert.equal(e['#upload-file'].disabled, false);
  e['#upload-file'].onchange();
  assert.equal(e['#upload-progress-row'].hidden, true);
  assert.equal(e['#upload-progress'].value, 0);
});

for (const failure of ['server', 'network', 'abort', 'timeout', 'invalid response']) {
  test(`${failure} failure resets progress and re-enables upload`, async () => {
    const {elements: e, requests, submit} = await setup();
    const done = submit();
    const xhr = requests[0];
    xhr.upload.onprogress({lengthComputable: true, loaded: 50, total: 100});
    if (failure === 'server' || failure === 'invalid response') {
      xhr.status = 409;
      xhr.response = failure === 'server' ? {error: 'File already exists.'} : null;
      xhr.onload();
    } else {
      xhr[{network: 'onerror', abort: 'onabort', timeout: 'ontimeout'}[failure]]();
    }
    await done;
    assert.equal(e['#upload-progress-row'].hidden, true);
    assert.equal(e['#upload-progress'].value, 0);
    assert.equal(e['#upload-status'].className, 'error');
    assert.equal(e['#upload-status'].textContent,
      failure === 'server' ? 'File already exists.' : 'Upload failed. Please retry.');
    assert.equal(e['#upload'].disabled, false);
    assert.equal(e['#upload-file'].disabled, false);
  });
}


test('dropdown sizes, compact metadata, and server-selected mode', async () => {
  const {elements: e, context} = await setup(async url => ({ok: true, json: async () =>
    url === '/api/videos' ? {files: ['bird.mp4'], sizes: {'bird.mp4': 18400000}} :
    url === '/api/status' ? {state: 'streaming', filename: 'bird.mp4', mode: 'Transcoding'} :
    {width: 1280, height: 720, codec: 'h264', fps: 25, duration: 12.5}}));
  assert.equal(e['#video'].options[0].text, 'bird.mp4 (18.4 MB)');
  assert.equal(e['#video'].options[0].value, 'bird.mp4');
  assert.equal(e['#metadata'].textContent, '1280×720 · h264 · 25.00 FPS · 12.5 s');
  assert.equal(e['#mode'].textContent, 'Transcoding');
  vm.runInContext("show({state: 'streaming', mode: 'Stream copy'})", context);
  assert.equal(e['#mode'].textContent, 'Stream copy');
  vm.runInContext("show({state: 'stopped', mode: 'Idle'})", context);
  assert.equal(e['#mode'].textContent, 'Idle');
});

test('deletion requires confirmation, refreshes selection, and handles refusal', async () => {
  let files = ['first.mp4', 'second.mp4'];
  let deletes = 0;
  let refused = false;
  const {elements: e, context} = await setup(async (url, options) => {
    if (options?.method === 'DELETE') {
      deletes++;
      assert.equal(url, `/api/videos/${files[0]}`);
      if (refused) return {ok: false, json: async () => ({error: 'Stop it first.'})};
      files.shift();
      return {ok: true, json: async () => ({})};
    }
    return {ok: true, json: async () => url === '/api/videos' ? {files} : {codec: 'h264'}};
  });
  context.confirm = () => false;
  await e['#delete-video'].onclick();
  assert.equal(deletes, 0);
  context.confirm = () => true;
  await e['#delete-video'].onclick();
  assert.equal(deletes, 1);
  assert.equal(e['#video'].value, 'second.mp4');
  assert.match(e['#metadata'].textContent, /h264/);
  assert.equal(e['#delete-status'].textContent, 'Deleted first.mp4.');
  refused = true;
  await e['#delete-video'].onclick();
  assert.equal(e['#delete-status'].textContent, 'Stop it first.');
  assert.equal(e['#delete-status'].className, 'error');
  assert.equal(e['#delete-video'].disabled, false);
  refused = false;
  await e['#delete-video'].onclick();
  assert.equal(e['#video'].value, '');
  assert.equal(e['#metadata'].textContent, 'No video selected');
  assert.equal(e['#delete-video'].disabled, true);
});

test('late metadata response cannot replace the current selection details', async () => {
  const {elements: e, context} = await setup();
  let finishOld;
  context.fetch = () => new Promise(resolve => { finishOld = resolve; });
  e['#video'].value = 'old.mp4';
  const old = e['#video'].onchange();
  context.fetch = async () => ({ok: true, json: async () => ({codec: 'vp9'})});
  e['#video'].value = 'new.mp4';
  await e['#video'].onchange();
  finishOld({ok: true, json: async () => ({codec: 'h264'})});
  await old;
  assert.match(e['#metadata'].textContent, /vp9/);
});

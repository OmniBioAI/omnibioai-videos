import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const html = fs.readFileSync('portal/index.html', 'utf8');
const source = fs.readFileSync('portal/embed-appearance.js', 'utf8');
const bootstrap = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];

function loadProtocol({ referrer = 'https://webstudio.omnibioai.org/workbench', devOrigin = '' } = {}) {
  const listeners = new Map();
  const sent = [];
  const parent = { postMessage: (message, origin) => sent.push([message, origin]) };
  const root = { dataset: {}, style: { cssText: 'untouched' }, innerHTML: 'untouched' };
  const document = {
    referrer,
    documentElement: root,
    querySelector: () => ({ content: devOrigin }),
  };
  const window = {
    parent,
    document,
    URL,
    Set,
    Object,
    addEventListener: (type, handler) => listeners.set(type, handler),
    removeEventListener: (type) => listeners.delete(type),
  };
  vm.runInNewContext(source, { window, URL, Set, Object });
  return { api: window.OmniBioAIEmbedAppearance, listeners, parent, root, sent, window };
}

test('pre-paint bootstrap is before styles, allowlists values, marks embedding, and scrubs only private keys', () => {
  assert.ok(bootstrap);
  assert.ok(html.indexOf('<script>') < html.indexOf('<link rel="stylesheet"'));
  const location = {
    pathname: '/_svc/videos/',
    search: '?obai_theme=light&obai_accent=purple&obai_density=compact&obai_motion=reduce&obai_other=x&token=x&author=Jane&username=reader',
    hash: '#watch',
  };
  let replaced = '';
  const root = { dataset: {}, style: { cssText: 'untouched' }, innerHTML: 'untouched' };
  const parent = {};
  const window = {
    parent,
    location,
    history: { state: null, replaceState: (_state, _title, url) => { replaced = url; } },
    matchMedia: () => ({ matches: false }),
  };
  vm.runInNewContext(bootstrap, { window, document: { documentElement: root }, URLSearchParams, Set, Array });
  assert.deepEqual(root.dataset, { theme: 'light', accent: 'purple', density: 'compact', motion: 'reduce', embedded: 'true' });
  const clean = new URL(replaced, 'https://videos.example');
  assert.equal(clean.searchParams.has('obai_theme'), false);
  assert.equal(clean.searchParams.has('obai_other'), false);
  assert.equal(clean.searchParams.has('token'), false);
  assert.equal(clean.searchParams.get('author'), 'Jane');
  assert.equal(clean.searchParams.get('username'), 'reader');
  assert.equal(clean.hash, '#watch');
  assert.equal(root.style.cssText, 'untouched');
  assert.equal(root.innerHTML, 'untouched');
});

test('pre-paint bootstrap ignores invalid enum values', () => {
  const location = { pathname: '/', search: '?obai_theme=evil&obai_accent=red&obai_density=tiny&obai_motion=yes', hash: '' };
  const root = { dataset: {} };
  const window = {
    location,
    history: { state: null, replaceState() {} },
    matchMedia: () => ({ matches: true }),
  };
  window.parent = window;
  vm.runInNewContext(bootstrap, { window, document: { documentElement: root }, URLSearchParams, Set, Array });
  assert.deepEqual(root.dataset, { theme: 'dark', accent: 'teal', density: 'comfortable', motion: 'system', embedded: 'false' });
});

test('theme messages require exact production origin, parent source, exact keys, and allowed values', () => {
  const state = loadProtocol();
  assert.equal(JSON.stringify(state.sent), JSON.stringify([[{ type: 'omnibioai.embed.ready.v1' }, 'https://webstudio.omnibioai.org']]));
  const deliver = (origin, source, data) => state.listeners.get('message')({ origin, source, data });
  const valid = { type: 'omnibioai.theme.set.v1', theme: 'light', accent: 'purple', density: 'compact', motion: 'reduce' };
  deliver('https://evil.example', state.parent, valid);
  deliver('https://webstudio.omnibioai.org', {}, valid);
  deliver('https://webstudio.omnibioai.org', state.parent, { ...valid, token: 'x' });
  deliver('https://webstudio.omnibioai.org', state.parent, { ...valid, accent: 'red' });
  deliver('https://webstudio.omnibioai.org', state.parent, { ...valid, type: 'other' });
  assert.deepEqual(state.root.dataset, {});
  deliver('https://webstudio.omnibioai.org', state.parent, valid);
  assert.deepEqual(state.root.dataset, { theme: 'light', accent: 'purple', density: 'compact', motion: 'reduce' });
  assert.equal(state.root.style.cssText, 'untouched');
  assert.equal(state.root.innerHTML, 'untouched');
});

test('development origin is disabled by default and accepted only when explicitly configured exactly', () => {
  assert.deepEqual(loadProtocol({ referrer: 'http://localhost:5174/' }).sent, []);
  assert.equal(JSON.stringify(loadProtocol({ referrer: 'http://localhost:5174/', devOrigin: 'http://localhost:5174' }).sent),
    JSON.stringify([[{ type: 'omnibioai.embed.ready.v1' }, 'http://localhost:5174']]));
  assert.deepEqual(loadProtocol({ referrer: 'http://localhost:5174/', devOrigin: 'http://localhost:5174.attacker.example' }).sent, []);
});

test('vendored design tokens are launcher 1.1.0 without tooling or a nested lock', () => {
  const metadata = JSON.parse(fs.readFileSync('vendor/omnibioai-design-tokens/package.json', 'utf8'));
  assert.equal(metadata.version, '1.1.0');
  assert.equal(metadata.scripts, undefined);
  assert.equal(metadata.devDependencies, undefined);
  assert.equal(fs.existsSync('vendor/omnibioai-design-tokens/package-lock.json'), false);
  assert.equal(fs.readFileSync('portal/omnibioai-design-tokens.css', 'utf8'), fs.readFileSync('vendor/omnibioai-design-tokens/tokens.css', 'utf8'));
});

test('CSP is a single always header and permits only Studio as the cross-origin framer', () => {
  const digest = crypto.createHash('sha256').update(bootstrap).digest('base64');
  for (const name of ['nginx.public.conf', 'nginx.studio.conf']) {
    const nginx = fs.readFileSync(name, 'utf8');
    assert.equal((nginx.match(/add_header Content-Security-Policy/g) || []).length, 1);
    assert.match(nginx, /frame-ancestors 'self' https:\/\/webstudio\.omnibioai\.org[^\n]*" always;/);
    assert.ok(nginx.includes(`'sha256-${digest}'`), `${name} must allow the exact pre-paint script`);
  }
  assert.match(fs.readFileSync('portal/portal.css', 'utf8'), /html\[data-embedded="true"\] \.topbar \{ display: none; \}/);
});

// Headless render check for the generated album: no browser, no npm install.
//
//   node tools/check_album.js D:\dig\e\album\index.html
//
// It runs the album's real inline <script> against a stub DOM and reports how
// many day headers, tiles and <img> tags the renderer produced.  A JS typo or
// a payload change that breaks the filter pipeline shows up here in a second
// instead of as a blank page in the browser.
const fs = require('fs');

const path = process.argv[2];
if (!path) {
  console.error('usage: node tools/check_album.js <album/index.html>');
  process.exit(2);
}
const html = fs.readFileSync(path, 'utf8');

const dataMatch = html.match(/<script type="application\/json" id="data">([\s\S]*?)<\/script>/);
if (!dataMatch) { console.error('FAIL: no embedded data block'); process.exit(1); }
const DATA_TEXT = dataMatch[1];

const scriptMatch = html.match(/<script>\s*\(function[\s\S]*?<\/script>/);
if (!scriptMatch) { console.error('FAIL: no app script'); process.exit(1); }
const code = scriptMatch[0].replace(/^<script>/, '').replace(/<\/script>$/, '');

const els = {};
function makeEl(id) {
  const classes = {
    _set: new Set(),
    add(c) { this._set.add(c); },
    remove(c) { this._set.delete(c); },
    toggle(c, on) {
      const value = on === undefined ? !this._set.has(c) : !!on;
      value ? this._set.add(c) : this._set.delete(c);
      return value;
    },
    contains(c) { return this._set.has(c); },
  };
  return {
    id, style: {}, classList: classes, dataset: {}, textContent: '', _html: '',
    onclick: null, addEventListener() {}, appendChild() {},
    querySelectorAll() { return []; },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
  };
}
function getEl(id) {
  if (!els[id]) els[id] = makeEl(id);
  if (id === 'data') els[id].textContent = DATA_TEXT;
  return els[id];
}

const document = {
  getElementById: getEl,
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return makeEl('created'); },
  documentElement: { style: { setProperty() {} } },
};
const store = {};
const localStorage = {
  getItem(k) { return k in store ? store[k] : null; },
  setItem(k, v) { store[k] = v; },
};
const Blob = function () {};
const URL = { createObjectURL() { return 'blob:x'; }, revokeObjectURL() {} };
const window = { open() {} };

try {
  new Function('document', 'window', 'localStorage', 'Blob', 'URL', 'navigator', code)(
    document, window, localStorage, Blob, URL, {});
} catch (err) {
  console.error('FAIL: runtime error\n' + ((err && err.stack) || err));
  process.exit(2);
}

const list = els['list'] ? els['list'].innerHTML : '';
const report = {
  items: JSON.parse(DATA_TEXT).items.length,
  tiles: (list.match(/class="t[ "]/g) || []).length,
  days: (list.match(/class="day"/g) || []).length,
  imgs: (list.match(/<img /g) || []).length,
  counter: els['n'] ? els['n'].textContent : '',
  header: els['sub'] ? els['sub'].textContent.slice(0, 140) : '',
};
console.log(JSON.stringify(report, null, 1));
if (!report.tiles) { console.error('FAIL: no tiles rendered'); process.exit(3); }

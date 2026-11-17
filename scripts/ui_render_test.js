// Render smoke test: loads app.js against a tiny DOM stub and feeds it real
// data captured from the running stack. Catches template errors and undefined
// fields without needing a browser.
"use strict";
const fs = require("fs");
const vm = require("vm");

const here = __dirname + "/../clusterproof/ui/static/";
const appSrc = fs.readFileSync(here + "app.js", "utf8");

class El {
  constructor(tag = "div") {
    this.tag = tag; this.className = ""; this._html = ""; this.hidden = false;
    this.dataset = {}; this.children = []; this.listeners = {}; this.classList = {
      add: (c) => { this.className += " " + c; },
      remove: (c) => { this.className = this.className.replace(c, ""); },
      toggle: (c, on) => { on ? this.classList.add(c) : this.classList.remove(c); },
      contains: (c) => this.className.includes(c),
    };
  }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  set textContent(v) { this._html = String(v); }
  get textContent() { return this._html; }
  set outerHTML(v) { this._html = v; }
  get outerHTML() { return this._html; }
  querySelector() { return new El(); }
  querySelectorAll() { return []; }
  addEventListener(ev, fn) { this.listeners[ev] = fn; }
  appendChild(c) { this.children.push(c); return c; }
  remove() {}
  showModal() {}
  closest() { return new El(); }
}

const registry = {};
const hostRegistry = {};
function makeEl(sel) {
  if (!registry[sel]) registry[sel] = new El();
  return registry[sel];
}
class Host extends El {
  querySelector(sel) { return makeHostEl(sel); }
  querySelectorAll() { return []; }
}
function makeHostEl(sel) {
  if (!hostRegistry[sel]) hostRegistry[sel] = new El();
  return hostRegistry[sel];
}
const document = {
  querySelector: (sel) => makeEl(sel),
  querySelectorAll: () => [],
  createElement: (tag) => new El(tag),
  getElementById: (id) => makeEl("#" + id),
  body: new El("body"),
  addEventListener() {},
};

const state = { candidate: null, check: null, candidateId: null, runId: null,
                overview: null, rowTable: "accounts" };
const sandbox = {
  document, console, state,
  setTimeout, clearTimeout,
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
  FormData: class { constructor() {} entries() { return []; } },
  window: {},
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
// Expose internals for the test by appending an export line.
vm.runInContext(
  appSrc + `
;globalThis.__internals = {verdictBlock, statRow, ledgerBlock, weightsTable,
  findingsBlock, renderChanges, renderDesign, shortfallBlock};
globalThis.__renderCandidate = (cand, check) => {
  state.candidate = cand; state.check = check;
  state.candidateId = cand.candidate_id; state.runId = cand.run_id;
  renderCandidate();
};`, sandbox);

const internals = sandbox.__internals;
const shortfallBlock = internals.shortfallBlock;
const samples = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
let failures = 0;
function expect(name, fn) {
  try {
    const html = fn();
    if (typeof html !== "string" || !html.length) throw new Error("empty output");
    if (/undefined|\[object Object\]|NaN/.test(html)) {
      const m = html.match(/.{0,60}(undefined|\[object Object\]|NaN).{0,60}/);
      throw new Error("suspicious output: " + m[0]);
    }
    if (/&lt;script/.test(html)) throw new Error("unescaped script");
  } catch (err) {
    failures++;
    console.error(`  [FAIL] ${name}: ${err.message}`);
  }
}

console.log("UI render smoke test");
for (const [label, data] of Object.entries(samples)) {
  if (!data.candidate) continue;
  const cand = data.candidate;
  const check = data.check;
  const pub = data.published;
  expect(`${label}: verdict`, () => internals.verdictBlock(check, pub));
  expect(`${label}: stats`, () => internals.statRow(cand, check));
  expect(`${label}: ledger`, () => internals.ledgerBlock(cand));
  expect(`${label}: weights`, () => internals.weightsTable(cand));
  expect(`${label}: findings`, () => internals.findingsBlock(check));
  expect(`${label}: shortfall`, () => shortfallBlock(cand));
  expect(`${label}: full candidate render`, () => {
    Object.keys(hostRegistry).forEach((k) => delete hostRegistry[k]);
    registry["#candidate"] = Object.assign(new Host(), { hidden: false });
    sandbox.document.querySelector = (sel) =>
      sel === "#candidate" ? registry["#candidate"] : makeEl(sel);
    sandbox.__renderCandidate(cand, check);
    const parts = [".findings", ".shortfall-block"].map(
      (k) => hostRegistry[k]?.innerHTML || "");
    if (parts.some((p) => !p)) throw new Error("missing card content");
    return parts.join("");
  });
}
internals.renderDesign(samples.overview_src);
internals.renderChanges(samples.changes);
for (const sel of ["#relations", "#strata", "#changes"]) {
  expect(`dom ${sel}`, () => registry[sel] ? registry[sel].innerHTML : "");
}
expect("xss escaping", () => internals.ledgerBlock({
  payload: { ledger: { removed: [{ account_id: "<img src=x onerror=alert(1)>",
    stratum_id: "<b>", reason: "account_withdrawn", replaced_by: null }],
    added: [], rehomed: [], kept: [] } },
}));

if (failures) { console.error(`${failures} render failures`); process.exit(1); }
console.log("  all renderers produced sane markup");

/* ClusterProof console — no framework, no build step. */
"use strict";

const state = {
  overview: null,
  runId: null,
  candidateId: null,
  candidate: null,
  check: null,
  releaseId: null,
  rowTable: "accounts",
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const fmt = (n) => (n === null || n === undefined) ? "—" : Number(n).toLocaleString();
const pct = (n) => (n === null || n === undefined) ? "—" : `${(n * 100).toFixed(1)}%`;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const err = new Error(data.error || `request failed (${res.status})`);
    err.data = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(message, bad = false) {
  let el = $(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), bad ? 8000 : 4200);
}

/* ------------------------------------------------------------------ render */
function renderUpstream(health) {
  const el = $("#upstream");
  el.innerHTML = ["sampler", "checker", "publisher"].map((name) => {
    const up = health.upstream?.[name];
    const ok = up?.ok;
    return `<span title="${esc(up?.detail?.error || "reachable")}">
      <span class="dot ${ok ? "ok" : "bad"}"></span>${esc(name)}</span>`;
  }).join("");
}

function renderRuns(runs) {
  const el = $("#runs");
  if (!runs.length) {
    el.innerHTML = `<p class="none">No runs yet — create one to begin.</p>`;
    return;
  }
  el.innerHTML = runs.map((r) => `
    <button class="run-item ${r.run_id === state.runId ? "active" : ""}"
            data-run="${esc(r.run_id)}">
      <span class="t"><span>${esc(r.label)}</span>
        <span class="badge ${r.state === "published" ? "ok" : "neutral"}">${esc(r.state)}</span></span>
      <span class="s">
        <span class="mono">${esc(r.run_id)}</span>
        <span>seed ${esc(r.seed)}</span>
        <span>target ${fmt(r.sample_size)}</span>
        ${r.mode === "topup" ? `<span class="badge">top-up</span>` : ""}
      </span>
    </button>`).join("");
  $$(".run-item", el).forEach((b) =>
    b.addEventListener("click", () => loadRun(b.dataset.run)));
}

function verdictBlock(check, published) {
  if (published) {
    return `<div class="verdict pass"><strong>Published</strong>
      ${esc(published.release_id)} · ${fmt(published.rows)} rows written ·
      signature <span class="mono">${esc(published.signature.slice(0, 16))}…</span></div>`;
  }
  if (!check) {
    return `<div class="verdict">Not checked yet. Run the checker before publishing.</div>`;
  }
  const waived = check.waived || [];
  const errors = check.findings.filter((f) => f.severity === "error" && !f.waived);
  const warns = check.findings.filter((f) => f.severity === "warning");
  const errCodes = errors.map((f) => f.code);
  if (errors.length) {
    return `<div class="verdict fail"><strong>Blocked by ${errors.length} integrity
      finding${errors.length > 1 ? "s" : ""}</strong>
      ${esc(errCodes.join(", "))}. Publishing will be refused until these are
      resolved by changing the source or rebuilding — none of these can be
      waived.</div>`;
  }
  if (waived.length) {
    return `<div class="verdict warn"><strong>Publishable with an accepted conflict</strong>
      ${waived.length} representativeness conflict(s) accepted:
      ${waived.map((w) => `<span class="mono">${esc(w.code)}</span>`).join(", ")}.
      ${warns.length ? `${warns.length} warning(s) remain visible below.` : ""}</div>`;
  }
  if (warns.length) {
    return `<div class="verdict warn"><strong>Checker passed with ${warns.length}
      warning${warns.length > 1 ? "s" : ""}</strong>
      Warnings are advisory; they stay attached to the release manifest.</div>`;
  }
  return `<div class="verdict pass"><strong>Checker passed</strong>
    No withdrawals, closure gaps or stratum problems detected.</div>`;
}

function shortfallBlock(cand) {
  const p = cand.payload;
  const gap = p.design.target_size - p.members.length;
  if (gap <= 0) {
    return `<p class="none">Delivered the full target of ${fmt(p.design.target_size)} members.</p>`;
  }
  const items = (p.shortfalls || []).map((s) => `
    <div class="item"><span class="badge warn">short</span>
      <div><span class="acct">${esc(s.stratum_id || "all strata")}</span>
        <span class="badge neutral">${fmt(s.selected)} / ${fmt(s.quota)}</span>
        <div class="why">${esc(s.message)}</div></div></div>`);
  return `<div class="verdict warn"><strong>${gap} member slot${gap > 1 ? "s" : ""}
    could not be filled</strong>
    The target stayed at ${fmt(p.design.target_size)}; the sample delivers
    ${fmt(p.members.length)} instead of compensating with rows from another
    stratum.</div>
    <div class="ledger">${items.join("") ||
      `<p class="none">No per-stratum detail recorded.</p>`}</div>`;
}

function statRow(cand, check) {
  const p = cand.payload;
  const members = p.members.length;
  const target = p.design.target_size;
  const gap = target - members;
  const rows = Object.values(p.rows).reduce((a, b) => a + b.length, 0);
  const withdrawnOmitted = p.omitted.length;
  const errors = check ? check.findings.filter(
    (f) => f.severity === "error" && !f.waived).length : "—";
  const warns = check ? check.findings.filter((f) => f.severity === "warning").length : "—";
  return `
    <div class="stat"><div class="k">members</div>
      <div class="v">${fmt(members)} <small>/ ${fmt(target)} target</small></div></div>
    <div class="stat ${gap > 0 ? "warn" : ""}"><div class="k">size gap</div>
      <div class="v">${gap > 0 ? `−${fmt(gap)}` : "0"} <small>${gap > 0 ? "declared, not padded" : "complete"}</small></div></div>
    <div class="stat"><div class="k">dependent rows</div>
      <div class="v">${fmt(rows)}</div></div>
    <div class="stat"><div class="k">withdrawn excluded</div>
      <div class="v">${fmt(withdrawnOmitted)}</div></div>
    <div class="stat ${errors === "—" ? "" : errors ? "bad" : "ok"}"><div class="k">errors</div>
      <div class="v">${errors}</div></div>
    <div class="stat ${warns === "—" ? "" : warns ? "warn" : "ok"}"><div class="k">warnings</div>
      <div class="v">${warns}</div></div>`;
}

const REASON_TEXT = {
  initial_fill: ["added", "selected by seeded rank"],
  new_data: ["added", "row arrived after the parent build"],
  replacement: ["added", "same-stratum replacement"],
  account_withdrawn: ["removed", "subject withdrew the whole row"],
  required_dependency_withdrawn: ["removed", "a required dependent row was withdrawn"],
  stratum_disabled: ["removed", "its stratum was disabled"],
  moved_into_disabled_stratum: ["removed", "moved into a disabled stratum"],
  stratum_shrank: ["removed", "quota shrank with its usable population"],
  quota_shrank: ["removed", "quota shrank"],
};

function ledgerBlock(cand) {
  const { added = [], removed = [], rehomed = [], kept = [] } = cand.payload.ledger;
  if (!added.length && !removed.length && !rehomed.length) {
    return `<p class="none">${kept.length ? `All ${kept.length} members kept from the parent candidate — no churn.` : "Initial sample; all members are new."}</p>`;
  }
  const items = [];
  for (const rem of removed) {
    const [tag, why] = REASON_TEXT[rem.reason] || ["removed", rem.reason];
    const rep = rem.replaced_by
      ? ` → replaced by <span class="acct">${esc(rem.replaced_by)}</span> (same stratum ${esc(rem.stratum_id)})`
      : ` → <strong>no replacement</strong>; ${esc(rem.stratum_id)} is short by one`;
    items.push(`<div class="item">
      <span class="badge bad">${esc(tag)}</span>
      <div><span class="acct">${esc(rem.account_id)}</span>
        <span class="badge neutral">${esc(rem.stratum_id)}</span>
        <div class="why">${esc(why)}${rep}</div></div></div>`);
  }
  for (const add of added) {
    if (add.reason === "initial_fill") continue;
    const [tag, why] = REASON_TEXT[add.reason] || ["added", add.reason];
    const rep = add.replaces
      ? `replaces <span class="acct">${esc(add.replaces)}</span>`
      : `filling a vacancy in ${esc(add.stratum_id)}`;
    items.push(`<div class="item">
      <span class="badge ok">${esc(tag)}</span>
      <div><span class="acct">${esc(add.account_id)}</span>
        <span class="badge neutral">${esc(add.stratum_id)}</span>
        <div class="why">${esc(why)} · ${rep}</div></div></div>`);
  }
  for (const home of rehomed) {
    items.push(`<div class="item">
      <span class="badge warn">moved</span>
      <div><span class="acct">${esc(home.account_id)}</span>
        <span class="badge neutral">${esc(home.from)}</span>
        <span class="arrow">→</span>
        <span class="badge neutral">${esc(home.to)}</span>
        <div class="why">stratification attributes changed; membership was kept</div></div></div>`);
  }
  return `<div class="ledger">${items.join("")}</div>`;
}

function weightsTable(cand) {
  const rows = cand.payload.weights.strata;
  const body = rows.map((w) => {
    const delta = w.weight_delta;
    const dcls = delta === null ? "" : delta > 0 ? "warn" : delta < 0 ? "ok" : "";
    return `<tr>
      <td>${esc(w.stratum_id)}</td>
      <td class="num">${fmt(w.population)}${w.previous_population !== null && w.previous_population !== undefined && w.previous_population !== w.population
        ? ` <span class="badge neutral">was ${fmt(w.previous_population)}</span>` : ""}</td>
      <td class="num">${fmt(w.selected)} / ${fmt(w.quota)}</td>
      <td class="num">${w.weight === null ? "—" : fmt(w.weight)}</td>
      <td class="num">${w.previous_weight === null || w.previous_weight === undefined ? "—" : fmt(w.previous_weight)}</td>
      <td class="num">${delta === null || delta === undefined ? "—"
        : `<span class="badge ${dcls}">${delta > 0 ? "+" : ""}${fmt(delta)}</span>`}</td>
    </tr>`;
  }).join("");
  return `<table class="table weights">
    <thead><tr><th>stratum</th><th class="num">population</th>
      <th class="num">members / quota</th><th class="num">weight</th>
      <th class="num">prev</th><th class="num">Δ</th></tr></thead>
    <tbody>${body}</tbody></table>
    <p class="hint">Weight = population ÷ members included. Falling membership in
      a fixed-population stratum raises its weight; that is disclosed here rather
      than hidden by holding the row count constant.</p>`;
}

function findingsBlock(check) {
  if (!check) return `<p class="none">No check recorded yet.</p>`;
  if (!check.findings.length) return `<p class="none">No findings.</p>`;
  return `<div class="findings">` + check.findings.map((f) => {
    const show = (v) => {
      if (v === null || v === undefined) return "—";
      if (Array.isArray(v)) {
        return v.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x)))
          .join(", ");
      }
      return typeof v === "object" ? JSON.stringify(v) : String(v);
    };
    const detail = Object.entries(f.detail || {})
      .filter(([k]) => k !== "message")
      .map(([k, v]) => `${esc(k)}=${esc(show(v))}`)
      .join(" · ");
    const waived = f.waived
      ? `<div class="note"><strong>Accepted by operator:</strong> ${esc(f.waived.note)}
           <span class="mono">(${esc(f.waived.at || "")})</span></div>` : "";
    return `<div class="finding ${esc(f.severity)} ${f.waived ? "waived" : ""}">
      <div class="fh"><span class="badge ${f.severity === "error" ? "bad" : f.severity === "warning" ? "warn" : "neutral"}">
        ${esc(f.severity)}</span>
        <span class="code">${esc(f.code)}</span>
        ${f.waived ? `<span class="badge waived">waived</span>` : ""}</div>
      <div>${esc(f.message)}</div>
      ${detail ? `<div class="mono" style="font-size:.7rem;color:var(--muted)">${detail}</div>` : ""}
      ${waived}</div>`;
  }).join("") + `</div>`;
}

function rowsBlock(cand) {
  const tables = Object.keys(cand.payload.rows);
  const active = tables.includes(state.rowTable) ? state.rowTable : tables[0];
  $("#row-tabs").innerHTML = tables.map((t) => `
    <button class="tab small ${t === active ? "active" : ""}" data-table="${esc(t)}">
      ${esc(t)} <span class="badge neutral">${fmt(cand.payload.rows[t].length)}</span></button>`).join("");
  $$("#row-tabs .tab").forEach((b) => b.addEventListener("click", () => {
    state.rowTable = b.dataset.table;
    renderCandidate();
  }));
  const rows = cand.payload.rows[active];
  if (!rows.length) { $(".rows").innerHTML = `<p class="none" style="padding:.6rem">No rows.</p>`; return; }
  const cols = Object.keys(rows[0]);
  $(".rows").innerHTML = `<table><thead><tr>${cols.map((c) =>
    `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>` +
    rows.map((r) => `<tr>${cols.map((c) =>
      `<td>${esc(r[c])}</td>`).join("")}</tr>`).join("") +
    `</tbody></table>`;
}

function renderCandidate() {
  const cand = state.candidate;
  if (!cand) {
    $("#candidate").hidden = true;
    $("#candidate-empty").hidden = false;
    return;
  }
  $("#candidate-empty").hidden = true;
  const host = $("#candidate");
  host.hidden = false;
  host.innerHTML = document.getElementById("tpl-candidate").innerHTML;
  const p = cand.payload;
  const published = (cand.events || []).filter((e) => e.kind === "published").pop();
  const check = state.check;

  $(".cand-id", host).textContent = cand.candidate_id;
  $(".cand-sub", host).innerHTML =
    `${esc(p.run.label)} · seed <span class="mono">${esc(p.run.seed)}</span>
     · ${esc(p.run.mode)}${p.parent ? ` of <span class="mono">${esc(p.parent.candidate_id)}</span>` : ""}
     · built ${esc(cand.built_at)}`;
  $(".verdict", host).outerHTML = verdictBlock(check, published?.detail);
  $(".statrow", host).innerHTML = statRow(cand, check);
  $(".shortfall-block", host).innerHTML = shortfallBlock(cand);
  $(".ledger", host).closest(".card").querySelector(".ledger").outerHTML =
    ledgerBlock(cand);
  $(".weights", host).outerHTML = weightsTable(cand);
  $(".findings", host).outerHTML = findingsBlock(check);
  rowsBlock(cand);

  const publishBtn = $('[data-act="publish"]', host);
  publishBtn.disabled = !!published || !check || !check.publishable;
  publishBtn.title = published ? "already published"
    : !check ? "run the checker first"
    : !check.publishable ? "blocked by integrity findings" : "";
  $('[data-act="check"]', host).addEventListener("click", recheck);
  $('[data-act="publish"]', host).addEventListener("click", publish);
  $('[data-act="waive"]', host).addEventListener("click", openWaiver);
}

/* --------------------------------------------------------------- data load */
async function refreshOverview() {
  const [health, overview] = await Promise.all([
    api("/api/health"), api("/api/overview"),
  ]);
  state.overview = overview;
  renderUpstream(health);
  renderRuns(overview.runs);
  const previous = $("#parent-run").value;
  $("#parent-run").innerHTML = overview.runs
    .map((r) => `<option value="${esc(r.run_id)}">${esc(r.label)} · ${esc(r.run_id)}</option>`)
    .join("");
  if (previous && overview.runs.some((r) => r.run_id === previous)) {
    $("#parent-run").value = previous;
  }
  renderDesign(overview);
  renderChanges(overview.source_changes);
}

function renderDesign(overview) {
  const rel = overview.design.config.relations;
  $("#relations").innerHTML = `<thead><tr><th>child</th><th>parent</th>
    <th>required</th><th>state</th></tr></thead><tbody>` +
    rel.map((r) => `<tr><td class="mono">${esc(r.child_table)}</td>
      <td class="mono">${esc(r.parent_table)}.${esc(r.parent_col)}</td>
      <td>${r.required ? "yes" : "optional"}</td>
      <td><span class="badge ${r.enabled ? "ok" : "neutral"}">${r.enabled ? "enabled" : "disabled"}</span></td></tr>`).join("") +
    `</tbody>`;
  const strata = overview.design.config.strata;
  $("#strata").innerHTML = `<thead><tr><th>stratum</th><th class="num">floor</th>
    <th class="num">eligible now</th><th>state</th></tr></thead><tbody>` +
    strata.map((s) => {
      const live = overview.frame.strata.find((x) => x.stratum_id === s.stratum_id);
      const eligible = live ? live.eligible : "—";
      const short = live && live.eligible < s.min_rows;
      return `<tr><td class="mono">${esc(s.stratum_id)}</td>
        <td class="num">${fmt(s.min_rows)}</td>
        <td class="num">${fmt(eligible)}${short ? ` <span class="badge warn">short</span>` : ""}</td>
        <td><span class="badge ${s.enabled ? "ok" : "neutral"}">${s.enabled ? "enabled" : "disabled"}</span></td></tr>`;
    }).join("") + `</tbody>`;
}

function renderChanges(changes) {
  if (!changes.length) {
    $("#changes").innerHTML = `<p class="none">No source changes recorded.</p>`;
    return;
  }
  $("#changes").innerHTML = changes.map((c) => {
    const detail = c.detail || {};
    let text = detail.note || "";
    if (c.kind === "insert") text = `${(detail.account_ids || []).length} new account(s)`;
    if (c.kind === "relation") text = `enabled=${detail.enabled}, required=${detail.required}`;
    if (c.kind === "stratum") text = detail.min_rows !== undefined
      ? `min_rows=${detail.min_rows}, enabled=${detail.enabled}`
      : `${detail.from} → ${detail.to}`;
    return `<div class="tl"><span class="at">${esc((c.at || "").replace("T", " ").replace("Z", ""))}</span>
      <div><span class="kind">${esc(c.kind)}</span>
        <span class="mono">${esc(c.target)}</span>
        <div class="hint">${esc(text)}</div></div></div>`;
  }).join("");
}

async function loadRun(runId) {
  state.runId = runId;
  renderRuns(state.overview.runs);
  try {
    const data = await api(`/api/runs/${runId}`);
    const cands = data.candidates;
    if (!cands.length) {
      state.candidate = null; state.check = null;
      renderCandidate();
      toast("Run has no candidate yet — build one.");
      return;
    }
    const latest = cands[cands.length - 1];
    state.candidateId = latest.candidate_id;
    state.candidate = latest;
    state.check = latest.live_check || null;
    if (!state.check) {
      for (const c of cands) {
        const ev = (c.events || []).filter((e) => e.kind === "check").pop();
        if (c.candidate_id === latest.candidate_id && ev) state.check = ev.detail;
      }
    }
    renderCandidate();
  } catch (err) { toast(err.message, true); }
}

async function recheck(waivers = []) {
  if (!state.candidateId) return;
  try {
    const res = await api(`/api/candidates/${state.candidateId}/check`,
      { method: "POST", body: { waivers } });
    state.check = { findings: res.findings, waived: res.waived || [],
                    verdict: res.verdict, publishable: res.verdict === "pass" };
    await refreshOverview();
    await loadRun(state.runId);
    toast(res.verdict === "pass" ? "Checker passed." : "Checker found blocking issues.",
      res.verdict !== "pass");
  } catch (err) {
    toast(err.message, true);
  }
}

async function publish() {
  if (!state.candidateId) return;
  try {
    const res = await api("/api/publish", { method: "POST",
      body: { candidate_id: state.candidateId } });
    toast(`Published ${res.release.release_id} · ${fmt(res.release.member_count)} members.`);
    await refreshOverview();
    await loadRun(state.runId);
    await loadRelease(res.release.release_id);
    switchView("releases");
  } catch (err) {
    toast(err.message, true);
  }
}

function openWaiver() {
  const check = state.check;
  if (!check) { toast("Run the checker first.", true); return; }
  const wavable = check.findings.filter((f) =>
    f.severity === "error" && !f.waived && f.code === "stratum_floor_unmet");
  if (!wavable.length) {
    toast("Nothing waivable here. Only an unmet stratum floor caused by a " +
      "genuinely exhausted population can be accepted — integrity findings cannot.",
      true);
    return;
  }
  const dlg = document.createElement("dialog");
  dlg.className = "waive";
  dlg.innerHTML = `<form method="dialog"><h2>Accept a representativeness conflict</h2>
    <p class="hint">This records your reason on the candidate and in the release
      manifest. The finding stays visible; it is not deleted.</p>
    <p class="mono" style="font-size:.78rem">${wavable.map((f) => esc(f.message)).join("<br>")}</p>
    <label>Reason (required)
      <textarea placeholder="e.g. guild:west has only 2 accounts; one withdrew and no eligible in-stratum row remains. We accept 1 member and will revisit when new west rows arrive."></textarea>
    </label>
    <div class="actions">
      <button class="ghost" value="cancel">Cancel</button>
      <button class="primary" value="ok">Accept &amp; re-check</button>
    </div></form>`;
  document.body.appendChild(dlg);
  dlg.showModal();
  dlg.addEventListener("close", async () => {
    if (dlg.returnValue === "ok") {
      const note = $("textarea", dlg).value.trim();
      if (!note) { toast("A reason is required to accept a conflict.", true); dlg.remove(); return; }
      await recheck([{ code: "stratum_floor_unmet", note }]);
    }
    dlg.remove();
  });
}

/* ---------------------------------------------------------------- releases */
function renderReleaseList(releases) {
  const el = $("#release-list");
  if (!releases.length) {
    el.innerHTML = `<p class="none">Nothing published yet.</p>`;
    return;
  }
  el.innerHTML = releases.map((r) => `
    <button class="run-item ${r.release_id === state.releaseId ? "active" : ""}"
            data-release="${esc(r.release_id)}">
      <span class="t"><span class="mono">${esc(r.release_id)}</span>
        ${r.supersedes ? `<span class="badge warn">supersedes</span>` : ""}</span>
      <span class="s"><span>${esc(r.published_at)}</span>
        <span class="mono">${esc(r.candidate_id)}</span></span>
    </button>`).join("");
  $$(".run-item", el).forEach((b) =>
    b.addEventListener("click", () => loadRelease(b.dataset.release)));
}

async function loadRelease(releaseId) {
  state.releaseId = releaseId;
  if (state.overview) renderReleaseList(state.overview.releases);
  try {
    const { release, rows } = await api(`/api/releases/${releaseId}`);
    const m = release.manifest;
    const manifestWaived = m.waived_conflicts || [];
    const manifestShortfalls = m.shortfalls || [];
    const ver = release.verification;
    const pull = release.withdrawals_since || [];
    const panel = $("#release-panel");
    panel.className = "panel wide";
    panel.innerHTML = `
      <p class="eyebrow">release</p>
      <h2 class="mono">${esc(release.release_id)}</h2>
      <p class="hint">Frozen ${esc(release.published_at)} · candidate
        <span class="mono">${esc(release.candidate_id)}</span>
        ${release.supersedes ? `· supersedes <span class="mono">${esc(release.supersedes)}</span>` : ""}</p>
      <div class="verdict ${ver.signature_valid ? "pass" : "fail"}">
        <strong>${ver.signature_valid ? "Signature valid" : "Signature mismatch"}</strong>
        Row counts ${ver.row_counts_match ? "match" : "do NOT match"} the manifest.
        ${(ver.leaked_withdrawals || []).length
          ? `<br><span class="badge bad">${ver.leaked_withdrawals.length} withdrawal(s) leaked into this release</span>
             — stop distribution and publish a corrected superseding release.`
          : `<br><span class="badge ok">No withdrawal known at freeze time appears in these rows</span>`}
        ${pull.length ? `<br><span class="badge warn">${pull.length} withdrawal(s) since publication</span>
          — publish a superseding release; this release is not edited.` : ""}
      </div>
      <div class="statrow">
        <div class="stat"><div class="k">members</div><div class="v">${fmt(m.member_count)}</div></div>
        <div class="stat"><div class="k">target</div><div class="v">${fmt(m.target_size)}</div></div>
        <div class="stat ${manifestShortfalls.length ? "warn" : ""}"><div class="k">shortfalls</div>
          <div class="v">${fmt(manifestShortfalls.length)}</div></div>
        <div class="stat ${manifestWaived.length ? "warn" : ""}"><div class="k">accepted conflicts</div>
          <div class="v">${fmt(manifestWaived.length)}</div></div>
        ${Object.entries(release.row_counts).map(([t, n]) =>
          `<div class="stat"><div class="k">${esc(t)}</div><div class="v">${fmt(n)}</div></div>`).join("")}
      </div>
      <div class="cand-grid">
        <section class="card"><h3>Assignment &amp; weights</h3>
          <table class="table"><thead><tr><th>stratum</th><th class="num">population</th>
            <th class="num">members</th><th class="num">weight</th></tr></thead><tbody>
            ${m.assignment.map((a) => `<tr><td>${esc(a.stratum_id)}</td>
              <td class="num">${fmt(a.population)}</td>
              <td class="num">${fmt(a.selected)}</td>
              <td class="num">${a.weight === null ? "—" : fmt(a.weight)}</td></tr>`).join("")}
          </tbody></table></section>
        <section class="card"><h3>Churn recorded at publication</h3>
          ${m.churn.removed.length || m.churn.added.length
            ? `<div class="ledger">${m.churn.removed.map((r) => `
                <div class="item"><span class="badge bad">removed</span>
                  <div><span class="acct">${esc(r.account_id)}</span>
                    <div class="why">${esc(r.reason)}${r.replaced_by ? ` → ${esc(r.replaced_by)}` : " → no replacement"}</div></div></div>`).join("")}
              ${m.churn.added.filter((a) => a.reason !== "initial_fill").map((a) => `
                <div class="item"><span class="badge ok">added</span>
                  <div><span class="acct">${esc(a.account_id)}</span>
                    <div class="why">${esc(a.reason)}${a.replaces ? ` · replaces ${esc(a.replaces)}` : ""}</div></div></div>`).join("")}
              </div>`
            : `<p class="none">No membership churn in this release.</p>`}
          ${manifestWaived.length ? `<h3 style="margin-top:.8rem">Accepted conflicts</h3>
            ${manifestWaived.map((w) => `<p class="hint"><span class="badge waived">${esc(w.code)}</span>
              ${esc(w.note)}</p>`).join("")}` : ""}
        </section>
      </div>
      <section class="card"><h3>Released rows (copy frozen at ${esc(release.published_at)})</h3>
        <div class="rows"><table><thead><tr><th>table</th><th>key</th>
          <th>stratum</th><th>row</th></tr></thead><tbody>
          ${rows.map((r) => `<tr><td class="plain">${esc(r.table)}</td>
            <td>${esc(r.key)}</td><td class="plain">${esc(r.stratum_id || "—")}</td>
            <td class="plain">${esc(JSON.stringify(r.row))}</td></tr>`).join("")}
        </tbody></table></div></section>`;
  } catch (err) { toast(err.message, true); }
}

/* -------------------------------------------------------------------- init */
function switchView(view) {
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  $$(".tab[data-view]").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  if (view === "releases" && state.overview) renderReleaseList(state.overview.releases);
}

$$(".tab[data-view]").forEach((t) =>
  t.addEventListener("click", () => switchView(t.dataset.view)));

$("#run-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const body = Object.fromEntries(fd.entries());
  body.sample_size = Number(body.sample_size);
  if (body.mode !== "topup") delete body.parent_run_id;
  try {
    const { run } = await api("/api/runs", { method: "POST", body });
    await api(`/api/runs/${run.run_id}/candidates`, { method: "POST", body: {} });
    toast(`Built candidate for ${run.label}.`);
    await refreshOverview();
    await loadRun(run.run_id);
  } catch (err) { toast(err.message, true); }
});

$('#run-form [name="mode"]').addEventListener("change", (ev) => {
  $(".parent-wrap").hidden = ev.target.value !== "topup";
  syncTopupTarget();
});

function syncTopupTarget() {
  const mode = $('#run-form [name="mode"]').value;
  const parent = $("#parent-run").value;
  if (mode === "topup" && parent) {
    const run = state.overview?.runs.find((r) => r.run_id === parent);
    if (run) $('#run-form [name="sample_size"]').value = run.sample_size;
    $('[name="sample_size"]').readOnly = true;
  } else {
    $('[name="sample_size"]').readOnly = false;
  }
}
$("#run-form").addEventListener("input", syncTopupTarget);
$("#parent-run").addEventListener("change", syncTopupTarget);

(async function init() {
  try {
    await refreshOverview();
    if (state.overview.runs.length) await loadRun(state.overview.runs[0].run_id);
    if (state.overview.releases.length) {
      await loadRelease(state.overview.releases[0].release_id);
    }
  } catch (err) {
    toast(`Console could not reach services: ${err.message}`, true);
  }
})();

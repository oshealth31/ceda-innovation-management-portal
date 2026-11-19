'use strict';
/* CEDA Innovation Management Portal (built on FundingLens AI). No inline handlers (strict CSP): all clicks/submits are delegated via data-action / data-form. */
const CURRENCY = 'BWP';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>'"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
const num = (n) => Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
const money = (n) => (n === null || n === undefined ? '-' : `${CURRENCY} ${num(n)}`);
const pct = (n) => (n === null || n === undefined ? '-' : Number(n).toFixed(1) + '%');
const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '');

let token = sessionStorage.getItem('fl_token') || '';
let me = null, current = null, viewing = null, page = 'dashboard';
let site = null, formFields = [];
const listState = { offset: 0, limit: 25, status: '' };

const STATUS = {
  draft: ['Draft', ''], under_review: ['Panel review', ''], request_information: ['Revise and resubmit', 'warn'],
  refer_committee: ['Adjudication', 'warn'], approve: ['Awarded', 'good'], decline: ['Declined', 'bad'],
};
const DECISION_LABEL = { request_information: 'Send back for revision', refer_committee: 'Refer to adjudication', approve: 'Award', decline: 'Decline' };
const ROLE_LABEL = { applicant: 'Submitter', officer: 'Evaluator', committee: 'Adjudicator', admin: 'Administrator' };
const roleLabel = (r) => ROLE_LABEL[r] || r;
const SECTORS = ['Agriculture', 'Digital and ICT', 'Education', 'Energy', 'Finance', 'Health', 'Manufacturing', 'Mining', 'Tourism', 'Transport and logistics', 'Other'];
const STAGES = ['Idea', 'Prototype', 'Pilot', 'Early revenue', 'Scaling'];
const IDEA_FIELDS = [['idea_title', 'Idea title', 'input'], ['problem', 'Problem you are solving', 'textarea'], ['solution', 'Your proposed solution', 'textarea'],
  ['target_users', 'Who will use or benefit from it?', 'textarea']];
const CRIT_STATUS = {
  pass: ['Met', 'good'], fail: ['Not met', 'bad'], mentioned: ['Mentioned (verify)', 'good'], negated: ['Negative mention', 'bad'],
  not_mentioned: ['Not mentioned', 'warn'], unknown: ['Cannot assess', ''], needs_review: ['Officer judgement', ''],
};
const METRICS = [['revenue', 'Revenue', 'm'], ['expenses', 'Expenses', 'm'], ['profit', 'Profit', 'm'], ['margin', 'Margin', 'p'],
  ['funding', 'Funding requested', 'm'], ['contribution', 'Owner contribution', 'm'], ['funding_to_contribution', 'Funding / contribution', 'x'],
  ['owner_contribution_pct', 'Owner share of cost', 'p'], ['expense_to_revenue_ratio', 'Expense / revenue', 'x']];
const ORIGIN = { document: 'read from document - unconfirmed', officer_override: 'confirmed by officer', application_form: 'from application form' };
const NAV = [
  ['dashboard', 'Dashboard', ['admin', 'officer', 'committee', 'applicant']],
  ['applications', 'Ideas', ['admin', 'officer', 'committee', 'applicant']],
  ['new', 'Submit an idea', ['admin', 'applicant']],
  ['review', 'Idea review', ['admin', 'officer', 'committee', 'applicant']],
  ['committee', 'Adjudication queue', ['admin', 'committee']],
  ['scoring', 'Scoring criteria', ['admin', 'officer', 'committee']],
  ['standards', 'Screening rules', ['admin', 'officer', 'committee']],
  ['programmes', 'Calls / programmes', ['admin', 'officer', 'committee']],
  ['knowledge', 'Policy Knowledge', ['admin', 'officer', 'committee']],
  ['management', 'Management Dashboard', ['admin', 'officer', 'committee']],
  ['notifications', 'Notifications', ['admin', 'officer', 'committee', 'applicant']],
  ['support', 'Support', ['admin', 'officer', 'committee', 'applicant']],
  ['help', 'Help and FAQs', ['admin', 'officer', 'committee', 'applicant']],
  ['content', 'Site content', ['admin']],
  ['formbuilder', 'Application form', ['admin']],
  ['users', 'Users', ['admin']],
  ['audit', 'Audit Trail', ['admin']],
  ['messages', 'Message log', ['admin']],
  ['settings', 'Settings', ['admin', 'officer', 'committee', 'applicant']],
];
const TITLES = Object.fromEntries(NAV.map((n) => [n[0], n[1]]));
const isStaff = () => me && me.role !== 'applicant';
const canEdit = () => me && ['admin', 'officer'].includes(me.role);
const EDITABLE = ['draft', 'under_review', 'request_information'];

/* ---------- API ---------- */
function errText(j, status) {
  if (j && Array.isArray(j.detail)) return j.detail.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
  return (j && j.detail) || `Request failed (${status})`;
}
async function api(path, opt = {}) {
  opt.headers = Object.assign({}, opt.headers);
  if (token) opt.headers.Authorization = 'Bearer ' + token;
  const r = await fetch(path, opt);
  let j = null;
  if ((r.headers.get('content-type') || '').includes('json')) { try { j = await r.json(); } catch (e) { /* ignore */ } }
  if (r.status === 401 && token) { logout(); throw new Error((j && j.detail) || 'Session expired - please sign in again'); }
  if (!r.ok) throw new Error(errText(j, r.status));
  return j;
}
const send = (path, body, method = 'POST') => api(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
async function openReport(kind) {
  if (!current) return;
  const w = kind === 'html' ? window.open('', '_blank') : null;
  try {
    const r = await fetch(`/api/applications/${current.id}/committee-report${kind === 'docx' ? '.docx' : ''}`, { headers: { Authorization: 'Bearer ' + token } });
    if (!r.ok) { let j = null; try { j = await r.json(); } catch (e) { /* ignore */ } throw new Error(errText(j, r.status)); }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    if (kind === 'html') { w.location = url; } else {
      const a = document.createElement('a'); a.href = url; a.download = `Innovation_Portal_Report_${current.id.slice(0, 8)}.docx`;
      document.body.appendChild(a); a.click(); a.remove();
    }
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (e) { if (w) w.close(); toast(e.message); }
}
function toast(m) { const t = $('toast'); t.textContent = m; t.style.display = 'block'; clearTimeout(window._tt); window._tt = setTimeout(() => { t.style.display = 'none'; }, 4000); }
const busy = (btn, on) => { if (btn) btn.disabled = on; };
const pill = (label, cls = '') => `<span class="pill ${cls}">${esc(label)}</span>`;
const statusPill = (s) => { const x = STATUS[s] || [s, '']; return pill(x[0], x[1]); };
const fmtMetric = (kind, v) => (v === null || v === undefined ? '-' : kind === 'm' ? money(v) : kind === 'p' ? pct(v) : Number(v).toFixed(2) + 'x');

/* ---------- auth / shell ---------- */
async function boot() {
  if (!token) return;
  try {
    me = await api('/api/me');
    $('login').classList.add('hidden'); $('portal').classList.remove('hidden');
    $('userInfo').textContent = `${me.username} - ${roleLabel(me.role)} - ${me.institution.name}`;
    $('nav').innerHTML = NAV.filter((n) => n[2].includes(me.role)).map((n) => `<button class="nav" data-action="nav" data-page="${n[0]}">${esc(n[1])}</button>`).join('');
    try { const h = await fetch('/api/health'); $('ver').textContent = 'v' + (await h.json()).version; } catch (e) { /* ignore */ }
    await Promise.all([loadSite(), loadFormFields()]);
    show('dashboard');
  } catch (e) { /* api() already logged out on 401 */ }
}
function logout() {
  token = ''; me = null; current = null; sessionStorage.removeItem('fl_token');
  $('portal').classList.add('hidden'); $('login').classList.remove('hidden');
}
async function show(id) {
  page = id;
  document.querySelectorAll('.nav').forEach((x) => x.classList.toggle('active', x.dataset.page === id));
  $('title').textContent = TITLES[id] || 'CEDA Innovation Management Portal';
  setMenu(false);
  $('page').innerHTML = '<div class="empty">Loading...</div>';
  try { await ({ dashboard: pDashboard, applications: pApplications, new: pNew, review: pReview, committee: pCommittee, scoring: pScoring, standards: pStandards,
    programmes: pProgrammes, knowledge: pKnowledge, management: pManagement, notifications: pNotifications, users: pUsers, audit: pAudit, messages: pMessages, settings: pSettings, support: pSupport, help: pHelp, content: pContent, formbuilder: pFormBuilder }[id])(); }
  catch (e) { $('page').innerHTML = `<div class="card bad">${esc(e.message)}</div>`; }
}

/* ---------- responsive helpers ---------- */
function drawMenu(open) {
  $('side').classList.toggle('open', open);
  document.body.classList.toggle('menu-open', open);
  const b = document.querySelector('[data-action="menu"].mobile-menu'); if (b) b.setAttribute('aria-expanded', open ? 'true' : 'false');
}
/* Opening the menu adds a history step, so the phone's Back button closes it instead of leaving the portal. */
function setMenu(open) {
  const was = $('side').classList.contains('open');
  if (open === was) return;
  drawMenu(open);
  if (open) history.pushState({ menu: true }, '');
  else if (history.state && history.state.menu) history.back();
}
window.addEventListener('popstate', () => { if ($('side').classList.contains('open')) drawMenu(false); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setMenu(false); });
/* Phones show tables as stacked cards: copy each column header onto its cells as a label. */
function labelTables(root) {
  root.querySelectorAll('table.table').forEach((t) => {
    const heads = [...t.querySelectorAll('thead th')].map((th) => th.textContent.trim());
    t.querySelectorAll('tbody tr').forEach((tr) => [...tr.children].forEach((td, i) => { if (heads[i] && !td.hasAttribute('data-label')) td.setAttribute('data-label', heads[i]); }));
  });
}
new MutationObserver(() => labelTables($('page'))).observe($('page'), { childList: true, subtree: true });

/* ---------- pages ---------- */
function appRows(items) {
  return items.map((a) => `<tr><td><b>${esc(a.idea_title || a.applicant_name)}</b><div class="small muted">${esc(a.applicant_name)} - ${esc(a.id.slice(0, 8))}</div></td>
    <td>${esc(a.sector || '-')}</td><td>${esc(a.stage || '-')}</td><td>${statusPill(a.status)}</td>
    <td>${isStaff() ? `${a.coverage_pct === null ? '-' : pct(a.coverage_pct)}<div class="small muted">${a.high_risks} high / ${a.medium_risks} med</div>` : ''}</td>
    <td>${esc(when(a.updated_at || a.created_at))}</td><td><button class="small" data-action="open-app" data-id="${esc(a.id)}">Open</button></td></tr>`).join('')
    || '<tr><td colspan="7" class="empty">No ideas yet.</td></tr>';
}
async function pDashboard() {
  const list = await api('/api/applications?limit=5');
  let stats = '';
  if (isStaff()) {
    const d = await api('/api/management/dashboard');
    const st = d.status;
    stats = `<div class="grid">
      <div class="card"><div class="muted">Ideas submitted</div><div class="stat">${d.applications}</div></div>
      <div class="card"><div class="muted">In panel review</div><div class="stat">${st.under_review || 0}</div></div>
      <div class="card"><div class="muted">Sent back for revision</div><div class="stat">${st.request_information || 0}</div></div>
      <div class="card"><div class="muted">In adjudication</div><div class="stat">${st.refer_committee || 0}</div></div>
      <div class="card"><div class="muted">Awarded / declined</div><div class="stat">${st.approve || 0} / ${st.decline || 0}</div></div></div>`;
  }
  const c = (site && site.content) || {};
  const top = `${c.announcement ? `<div class="banner warn"><b>Notice:</b> ${esc(c.announcement)}</div>` : ''}${!isStaff() && c.welcome ? `<div class="card welcome">${esc(c.welcome)}</div>` : ''}`;
  $('page').innerHTML = `${top}${stats}<div class="card"><div class="row between"><h2>${isStaff() ? 'Recent ideas' : 'My ideas'}</h2><button data-action="nav" data-page="applications">View all</button></div>
    <div class="table-wrap"><table class="table"><thead><tr><th>Idea</th><th>Sector</th><th>Stage</th><th>Status</th><th>${isStaff() ? 'Screening coverage' : ''}</th><th>Last update</th><th></th></tr></thead><tbody>${appRows(list.items)}</tbody></table></div></div>`;
}
async function pApplications() {
  const q = new URLSearchParams({ limit: listState.limit, offset: listState.offset });
  if (listState.status) q.set('status', listState.status);
  const d = await api('/api/applications?' + q);
  const last = Math.min(listState.offset + listState.limit, d.total);
  $('page').innerHTML = `<div class="card"><div class="row between"><h2>Ideas (${d.total})</h2>
    <div class="row"><select id="statusFilter" data-change="filter" style="width:auto"><option value="">All statuses</option>${Object.entries(STATUS).map(([k, v]) => `<option value="${k}" ${listState.status === k ? 'selected' : ''}>${esc(v[0])}</option>`).join('')}</select>
    ${me.role === 'officer' || me.role === 'committee' ? '' : '<button data-action="nav" data-page="new">+ Submit an idea</button>'}</div></div>
    <div class="table-wrap"><table class="table"><thead><tr><th>Idea</th><th>Sector</th><th>Stage</th><th>Status</th><th>${isStaff() ? 'Screening coverage' : ''}</th><th>Last update</th><th></th></tr></thead><tbody>${appRows(d.items)}</tbody></table></div>
    <div class="row between" style="margin-top:10px"><span class="small muted">${d.total ? listState.offset + 1 : 0}-${last} of ${d.total}</span>
    <div class="row"><button class="ghost small" data-action="apps-page" data-dir="-1" ${listState.offset === 0 ? 'disabled' : ''}>Previous</button><button class="ghost small" data-action="apps-page" data-dir="1" ${last >= d.total ? 'disabled' : ''}>Next</button></div></div></div>`;
}
function ideaFields(a) {
  const v = (k) => esc(a ? a[k] : '');
  const opt = (list, cur) => `<option value="">Choose...</option>${list.map((x) => `<option ${cur === x ? 'selected' : ''}>${esc(x)}</option>`).join('')}`;
  return `${IDEA_FIELDS.map(([k, l, kind]) => `<div class="field"><label>${esc(l)} *</label>${kind === 'input'
    ? `<input name="${k}" required maxlength="200" value="${v(k)}">` : `<textarea name="${k}" rows="3" required maxlength="5000">${v(k)}</textarea>`}</div>`).join('')}
    <div class="inline-form"><div class="field"><label>Sector *</label><select name="sector" required>${opt(SECTORS, a && a.sector)}</select></div>
    <div class="field"><label>Stage *</label><select name="stage" required>${opt(STAGES, a && a.stage)}</select></div></div>${extraFields(a)}`;
}
function extraFields(a) {
  const ans = (a && a.answers) || {};
  return formFields.map((f) => {
    const n = `x_${esc(f.key)}`; const v = ans[f.key]; const req = f.required ? ' required' : '';
    const lab = `<label>${esc(f.label)}${f.required ? ' *' : ''}</label>`;
    const help = f.help ? `<div class="small muted">${esc(f.help)}</div>` : '';
    let input;
    if (f.kind === 'textarea') input = `<textarea name="${n}" rows="3" maxlength="5000"${req}>${esc(v ?? '')}</textarea>`;
    else if (f.kind === 'select') input = `<select name="${n}"${req}><option value="">Choose...</option>${f.options.map((o) => `<option ${v === o ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
    else if (f.kind === 'yes_no') input = `<select name="${n}"${req}><option value="">Choose...</option><option value="yes" ${v === true ? 'selected' : ''}>Yes</option><option value="no" ${v === false ? 'selected' : ''}>No</option></select>`;
    else input = `<input name="${n}" type="${{ number: 'number', date: 'date', url: 'url' }[f.kind] || 'text'}"${f.kind === 'number' ? ' step="any"' : ''} maxlength="500" value="${esc(v ?? '')}"${req}>`;
    return `<div class="field">${lab}${input}${help}</div>`;
  }).join('');
}
const answersFrom = (d) => Object.fromEntries(Object.entries(d).filter(([k]) => k.startsWith('x_')).map(([k, v]) => [k.slice(2), v]));
function answersHTML(a) {
  const ans = a.answers || {}; const known = formFields.filter((f) => f.key in ans);
  const fmt = (v) => (v === true ? 'Yes' : v === false ? 'No' : String(v));
  const extra = Object.keys(ans).filter((k) => !formFields.some((f) => f.key === k));
  return known.length || extra.length ? `<div class="answers">${known.map((f) => `<p><b>${esc(f.label)}:</b> ${esc(fmt(ans[f.key]))}</p>`).join('')}${extra.map((k) => `<p><b>${esc(k)}:</b> ${esc(fmt(ans[k]))}</p>`).join('')}</div>` : '';
}
async function loadSite() { try { site = await (await fetch('/api/public/help')).json(); } catch (e) { site = null; } }
async function loadFormFields() { try { formFields = await api('/api/form-fields'); } catch (e) { formFields = []; } }
async function pNew() {
  const progs = (await api('/api/programmes')).filter((p) => p.enabled);
  $('page').innerHTML = `<div class="card"><h2>Submit an innovative idea</h2><p class="muted">Fields marked * are required. After saving, upload a short document describing your idea (PDF, DOCX or TXT) to submit it for review.</p>
    <form data-form="create-app">
    <div class="inline-form"><div class="field"><label>Company or team name *</label><input name="applicant_name" required minlength="2" maxlength="200"></div>
    <div class="field"><label>Call / programme</label><select name="programme_id">${progs.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}<option value="">Not selected</option></select></div></div>
    ${ideaFields(null)}
    <details><summary class="small muted">Funding details (optional)</summary><div class="inline-form">
    <div class="field"><label>Funding requested (${CURRENCY})</label><input name="funding_request" type="number" min="0" step="any" value="0"></div>
    <div class="field"><label>Own contribution (${CURRENCY})</label><input name="owner_contribution" type="number" min="0" step="any" value="0"></div></div></details>
    <button type="submit" style="margin-top:12px">Save idea</button></form></div>`;
}
const STEP_ORDER = ['draft', 'under_review', 'refer_committee', 'approve'];
function stepper(status) {
  const steps = [['draft', 'Draft'], ['under_review', 'Panel review'], ['refer_committee', 'Adjudication'], ['approve', status === 'decline' ? 'Declined' : 'Awarded']];
  const at = status === 'request_information' ? 1 : status === 'decline' ? 3 : STEP_ORDER.indexOf(status);
  return `<div class="steps">${steps.map(([k, l], i) => `<div class="step ${i < at ? 'done' : i === at ? (status === 'decline' ? 'bad' : status === 'request_information' ? 'warn' : 'now') : ''}">${esc(i === 1 && status === 'request_information' ? 'Revise and resubmit' : l)}</div>`).join('')}</div>`;
}
async function timelineCard(id) {
  const tl = await api(`/api/applications/${id}/timeline`);
  return `<div class="card"><h3>Timeline</h3>${tl.events.slice().reverse().map((e) => `<div class="finding"><b>${esc(e.kind === 'decision' ? (STATUS[e.status] || [e.message])[0] : e.message)}</b> <span class="small muted">${esc(when(e.at))}</span>${e.feedback ? `<p>${esc(e.feedback)}</p>` : ''}</div>`).join('')}</div>`;
}
function ideaCard(a) {
  return `<div class="card"><h3>The idea</h3><div class="small muted">${esc(a.sector || '-')} - ${esc(a.stage || '-')}</div>
    <p><b>Problem:</b> ${esc(a.problem || '-')}</p><p><b>Solution:</b> ${esc(a.solution || '-')}</p><p><b>Target users:</b> ${esc(a.target_users || '-')}</p>${answersHTML(a)}</div>`;
}
async function openApp(id) { viewing = null; current = await api('/api/applications/' + id); await show('review'); }
async function pReview() {
  if (!current) { $('page').innerHTML = '<div class="card empty">Select an application from the Applications page.</div>'; return; }
  current = await api('/api/applications/' + current.id);
  if (!isStaff()) return renderApplicantReview();
  await renderStaffReview();
}
function uploadCard() {
  if (!EDITABLE.includes(current.status) || (!isStaff() && current.status === 'under_review')) return `<div class="banner">Documents are locked while the idea is in "${esc((STATUS[current.status] || [current.status])[0])}".</div>${docsTable()}`;
  const label = isStaff() ? 'Upload &amp; screen' : current.status === 'request_information' ? 'Upload revised document &amp; resubmit' : 'Upload document &amp; submit';
  return `<div class="card"><h3>Documents</h3><div class="drop"><input id="file" type="file" accept=".pdf,.docx,.txt,.md,.csv">
    <p class="muted small">PDF (text-based, not scanned), DOCX, TXT, MD or CSV. Every upload is kept as a new version.</p>
    <button data-action="upload">${label}</button> <span id="uploadMsg" class="small muted"></span></div>
    ${docsTable()}</div>`;
}
function docsTable() {
  const d = current.documents || [];
  return d.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>v</th><th>File</th><th>Size</th><th>SHA-256</th><th>Uploaded</th></tr></thead><tbody>${d.map((x) => `<tr><td>${x.version}</td><td>${esc(x.name)}</td><td>${num(x.size_bytes)} B</td><td class="small">${esc(x.sha256.slice(0, 16))}...</td><td>${esc(when(x.created_at))}</td></tr>`).join('')}</tbody></table></div>` : '';
}
async function renderApplicantReview() {
  const a = current;
  const [chk, tl] = await Promise.all([api(`/api/applications/${a.id}/applicant-check`), timelineCard(a.id)]);
  const sentBack = a.status === 'request_information';
  $('page').innerHTML = `<div class="card"><div class="row between"><div><h2>${esc(a.idea_title || a.applicant_name)}</h2><div class="small muted">${esc(a.applicant_name)}</div></div>${statusPill(a.status)}</div>${stepper(a.status)}</div>
    ${sentBack ? '<div class="banner warn"><b>Your idea was sent back for revision.</b> Read the feedback in the timeline, update your idea below, then upload a revised document to resubmit.</div>' : ''}
    ${a.can_edit ? `<div class="card"><h3>Edit your idea</h3><form data-form="idea-edit">${ideaFields(a)}<button type="submit">Save changes</button></form></div>` : ideaCard(a)}
    ${a.status === 'draft' || sentBack ? `<div class="card"><h3>Before you submit</h3>${chk.issues.filter((i) => i.severity !== 'low').map((i) => `<div class="finding ${i.severity}"><b>${esc(i.severity.toUpperCase())}</b> ${esc(i.message)}</div>`).join('') || '<div class="banner">Everything required is in place.</div>'}</div>` : ''}
    ${uploadCard()}${tl}<div class="banner">Assigned evaluators will review your idea. You will be notified at every status change.</div>`;
}
function riskHTML(r) { return `<div class="finding ${esc(r.severity)}"><b>${esc(r.title)}</b> ${pill(r.severity, r.severity === 'high' ? 'bad' : r.severity === 'medium' ? 'warn' : '')} ${r.source ? `<span class="small muted">[${esc(r.source)}]</span>` : ''}<p>${esc(r.detail)}</p>${r.quote ? `<div class="quote">"${esc(r.quote)}" ${r.quote_verified ? pill('quote verified', 'good') : pill('quote not found in document', 'warn')}</div>` : ''}</div>`; }
async function renderStaffReview() {
  const a = current;
  let r = a.assessment, versions = [], dd = [], claims = [], decisions = [], assigned = [], scores = null, users = [];
  [versions, dd, claims, decisions, assigned, scores] = await Promise.all([api(`/api/applications/${a.id}/versions`), api(`/api/applications/${a.id}/due-diligence`),
    api(`/api/applications/${a.id}/claim-evidence`), api(`/api/applications/${a.id}/decisions`), api(`/api/applications/${a.id}/assignments`), api(`/api/applications/${a.id}/scores`)]);
  if (me.role === 'admin') users = (await api('/api/users')).filter((u) => u.role === 'officer' && u.active);
  const hist = viewing !== null && viewing !== a.assessment_version;
  if (hist) r = await api(`/api/applications/${a.id}/versions/${viewing}`);
  const elig = a.eligibility ? `<div class="banner ${a.eligibility.eligible ? '' : 'warn'}"><b>Programme limits (${esc(a.programme.name)}):</b> ${a.eligibility.eligible ? 'within limits' : esc(a.eligibility.reasons.join(' '))} <span class="muted">(owner share ${pct(a.eligibility.owner_contribution_pct)})</span></div>` : '';
  let h = `<div class="card"><div class="row between"><div><h2>${esc(a.idea_title || a.applicant_name)}</h2><div class="small muted">${esc(a.applicant_name)}</div>${statusPill(a.status)} ${a.programme ? pill(a.programme.name) : ''}</div>
    <div class="row">${versions.length ? `<select data-change="version" style="width:auto">${versions.map((v) => `<option value="${v.version}" ${(viewing ?? a.assessment_version) === v.version ? 'selected' : ''}>Assessment v${v.version} (${esc(v.trigger.replace('_', ' '))}, ${esc(when(v.created_at))})</option>`).join('')}</select>` : ''}
    <button class="ghost" data-action="nav" data-page="applications">Back</button></div></div>${stepper(a.status)}${elig}</div>`;
  h += `<div class="two">${ideaCard(a)}${assignCard(a, assigned, users)}</div>` + scoreCard(a, scores);
  if (hist) h += `<div class="banner warn">You are viewing historical assessment v${viewing} (read-only). <button class="small" data-action="latest-version">Back to latest</button></div>`;
  h += hist ? '' : uploadCard();
  if (r) {
    h += `<details class="screen" ${hist ? 'open' : ''}><summary><b>Automated document screening</b> <span class="small muted">screening coverage ${r.rubric.coverage_pct === null ? 'n/a' : pct(r.rubric.coverage_pct)} - ${r.risks.length} flag(s) - tap to expand</span></summary>`;
    const m = r.metrics, src = r.sources || {};
    h += `<div class="card"><h3>Financial analysis</h3>${r.needs_confirmation && r.needs_confirmation.length ? `<div class="banner warn">Figures read from the document and <b>not yet confirmed</b>: ${esc(r.needs_confirmation.join(', '))}. Check them against the source line below, then confirm or correct.</div>` : ''}
      <div class="metric-grid">${METRICS.map(([k, l, kind]) => `<div class="metric">${esc(l)}<b>${fmtMetric(kind, m[k])}</b><div class="src">${src[k] ? esc(ORIGIN[src[k].origin] || src[k].origin) : ''}</div>${src[k] && src[k].line ? `<div class="src">"${esc(src[k].line)}"</div>` : ''}</div>`).join('')}</div>
      ${canEdit() && !hist && EDITABLE.includes(a.status) ? `<form data-form="financials" class="inline-form" style="margin-top:12px"><div class="field"><label>Confirm / correct revenue</label><input name="revenue" type="number" min="0" step="any" value="${esc(a.overrides.revenue ?? m.revenue ?? '')}"></div>
      <div class="field"><label>Confirm / correct expenses</label><input name="expenses" type="number" min="0" step="any" value="${esc(a.overrides.expenses ?? m.expenses ?? '')}"></div>
      <div class="row"><button type="submit">Confirm figures</button><button type="button" class="ghost" data-action="clear-financials">Clear</button></div></form>` : ''}</div>`;
    h += `<div class="card"><h3>Assessment</h3><div class="banner">${esc((r.ai || {}).message || '')} <span class="small muted">mode: ${esc((r.ai || {}).mode || '')} ${esc((r.ai || {}).model || '')}</span></div>
      <p>${esc(r.executive_summary)}</p><p><b>Business model:</b> ${esc(r.business_model)}</p>
      ${r.strengths.length ? `<p><b>Strengths:</b></p><ul>${r.strengths.map((s) => `<li>${esc(s)}</li>`).join('')}</ul>` : ''}</div>`;
    const rb = r.rubric;
    h += `<div class="card"><div class="row between"><h3>Standards (rubric)</h3><div>Criteria coverage: <b>${rb.coverage_pct === null ? 'n/a' : pct(rb.coverage_pct)}</b></div></div>
      ${rb.mandatory_failures.length ? `<div class="banner bad"><b>Mandatory criteria not met:</b> ${esc(rb.mandatory_failures.join('; '))}</div>` : ''}
      ${rb.mandatory_unassessed.length ? `<div class="banner warn"><b>Mandatory criteria that could not be assessed:</b> ${esc(rb.mandatory_unassessed.join('; '))}</div>` : ''}
      <p class="small muted">"Mentioned" is a keyword match, not verified evidence. Coverage is an indicator for the officer, not a recommendation.</p>
      <div class="table-wrap"><table class="table"><thead><tr><th>Criterion</th><th>Result</th><th>Detail</th></tr></thead><tbody>${rb.criteria.map((c) => { const s = CRIT_STATUS[c.status] || [c.status, '']; return `<tr><td>${esc(c.label)}${c.mandatory ? ' <span class="pill warn">mandatory</span>' : ''}</td><td>${pill(s[0], s[1])}</td><td>${esc(c.detail)}${c.ai_note ? `<div class="quote">AI: ${esc(c.ai_note)}${c.ai_quote ? ` - "${esc(c.ai_quote)}" ${c.ai_quote_verified ? pill('verified', 'good') : pill('unverified', 'warn')}` : ''}</div>` : ''}</td></tr>`; }).join('')}</tbody></table></div></div>`;
    h += `<div class="card"><h3>Risks (${r.risks.length})</h3>${r.risks.map(riskHTML).join('') || '<div class="empty">No risks flagged.</div>'}
      <h3>Evidence gaps</h3>${r.evidence.map((e) => `<div class="finding"><b>${esc(e.claim)}</b> ${pill(e.status.replace('_', ' '), ['missing', 'unsupported', 'contradictory', 'needs_verification'].includes(e.status) ? 'warn' : 'good')} <span class="small muted">[${esc(e.source || '')}]</span><p>${esc(e.detail)}</p>${e.quote ? `<div class="quote">"${esc(e.quote)}" ${e.quote_verified ? pill('quote verified', 'good') : pill('quote not found', 'warn')}</div>` : ''}</div>`).join('') || '<div class="empty">None.</div>'}</div></details>`;
  } else if (!hist) h += '<div class="card empty">No screening yet. Upload a document above.</div>';
  if (!hist && r) {
    h += `<details class="screen"><summary><b>Follow-up questions and verified evidence</b> <span class="small muted">${dd.filter((q) => q.status === 'open').length} open question(s) - tap to expand</span></summary><div class="card"><h3>Due diligence</h3>${dd.map((q) => `<div class="finding ${q.priority === 'high' ? 'high' : 'medium'}" ${q.status === 'superseded' ? 'style="opacity:.55"' : ''}><b>${esc(q.priority.toUpperCase())} - ${esc(q.category)}</b> ${pill(q.status, q.status === 'answered' ? 'good' : '')}<p>${esc(q.question)}</p>
      ${q.status === 'superseded' ? '<p class="small muted">No longer applies to the latest assessment.</p>' : canEdit() ? `<textarea id="dd_${esc(q.id)}" rows="2" placeholder="Answer">${esc(q.answer)}</textarea><input id="dde_${esc(q.id)}" placeholder="Evidence note / document reference" value="${esc(q.evidence_note)}" style="margin-top:6px"><button class="small" style="margin-top:6px" data-action="dd-save" data-id="${esc(q.id)}">Save answer</button>` : `<p>${esc(q.answer)}</p>`}</div>`).join('') || '<div class="empty">No questions.</div>'}</div>`;
    h += `<div class="card"><h3>Officer-verified evidence</h3>${claims.map((c) => `<div class="finding"><b>${esc(c.claim)}</b> ${pill(c.evidence_status.replace('_', ' '))}<p class="small muted">${esc(c.source)} ${esc(c.note)}</p></div>`).join('') || '<div class="empty">None recorded.</div>'}
      ${canEdit() ? `<form data-form="claim" class="inline-form"><div class="field"><label>Claim</label><input name="claim" required minlength="3"></div><div class="field"><label>Status</label><select name="evidence_status"><option value="verified">verified</option><option value="partially_verified">partially verified</option><option value="unsupported">unsupported</option><option value="contradictory">contradictory</option><option value="unverified">unverified</option></select></div><div class="field"><label>Source document</label><input name="source"></div><div class="field"><label>Note</label><input name="note"></div><button type="submit">Add</button></form>` : ''}</div></details>`;
    const acts = a.allowed_actions || [];
    h += `<div class="card"><h3>Decision</h3><p class="small muted">Feedback written when sending back, awarding or declining is shown to the submitter.</p>${acts.length ? `<div class="field"><label>Feedback / rationale (required, at least 10 characters)</label><textarea id="reason" rows="3"></textarea></div><div class="field"><label>Conditions (optional)</label><input id="conditions"></div>
      <div class="row">${acts.map((d) => `<button class="${d === 'approve' ? 'ok' : d === 'decline' ? 'danger' : ''}" data-action="decide" data-decision="${d}">${esc(DECISION_LABEL[d])}</button>`).join('')}</div>`
      : `<div class="banner">${a.status === 'refer_committee' && a.referred_by === me.id ? 'You referred this idea, so a different adjudicator must decide it.' : 'No decision action is available to your role at this stage.'}</div>`}
      <p class="small muted">Decisions are recorded against assessment v${a.assessment_version}. The AI never makes the funding decision.</p>
      ${decisions.map((d) => `<div class="finding"><b>${esc(d.decision.replace('_', ' '))}</b> by ${esc(d.decided_by)} <span class="small muted">${esc(when(d.created_at))} - assessment v${d.assessment_version ?? '-'}</span><p>${esc(d.rationale)}</p>${d.conditions ? `<p><b>Conditions:</b> ${esc(d.conditions)}</p>` : ''}</div>`).join('')}</div>
      <div class="card"><h3>Reports</h3><div class="row"><button data-action="report-html">View committee report</button><button data-action="report-docx">Download Word report</button><button class="ghost" data-action="knowledge-context">Check institutional knowledge</button></div></div>`;
  }
  $('page').innerHTML = h;
}
function assignCard(a, assigned, users) {
  const names = assigned.map((x) => `${esc(x.username)} ${x.has_scored ? pill('scored', 'good') : pill('not scored')}`).join(', ') || '<span class="muted">No evaluators assigned yet.</span>';
  const ids = new Set(assigned.map((x) => x.reviewer_id));
  return `<div class="card"><h3>Evaluators</h3><p>${names}</p>${me.role === 'admin' ? `<form data-form="assign"><div class="row" style="flex-wrap:wrap">${users.map((u) => `<label class="small" style="margin-right:12px"><input type="checkbox" name="r" value="${u.id}" ${ids.has(u.id) ? 'checked' : ''}> ${esc(u.username)}</label>`).join('') || '<span class="small muted">Create users with the Evaluator role first.</span>'}</div>
    <button type="submit" class="small" style="margin-top:8px">Save assignment</button></form>` : ''}<p class="small muted">Evaluators only see ideas assigned to them.</p></div>`;
}
function scoreCard(a, sc) {
  if (!sc) return '';
  const crit = sc.criteria;
  let h = '<div class="card"><h3>Scoring</h3>';
  if (sc.can_score) {
    if (sc.panel) h += '<details><summary class="small">Add my own scores</summary>';
    h += `<p class="small muted">Score each criterion from 1 (weak) to 5 (excellent). Your scores are independent: other evaluators cannot see them.</p><form data-form="scores">${crit.map((c) => { const m = sc.mine[c.id] || {}; return `<div class="field"><label>${esc(c.label)} <span class="small muted">(weight ${c.weight})</span></label><div class="small muted">${esc(c.description)}</div>
      <div class="row"><select name="s_${esc(c.id)}" required style="width:auto"><option value="">-</option>${[1, 2, 3, 4, 5].map((n) => `<option ${m.score === n ? 'selected' : ''}>${n}</option>`).join('')}</select><input name="c_${esc(c.id)}" placeholder="Comment (optional)" maxlength="2000" value="${esc(m.comment || '')}"></div></div>`; }).join('')}
      <button type="submit">Save my scores</button></form>`;
    if (sc.panel) h += '</details>';
  }
  if (sc.my_total !== null) h += `<div class="banner">Your weighted score: <b>${sc.my_total} / 100</b></div>`;
  if (sc.panel) {
    const p = sc.panel;
    h += p.reviewers.length ? `${p.disagreement ? `<div class="banner warn"><b>Evaluators disagree:</b> totals differ by ${p.spread} points. Discuss before deciding.</div>` : ''}
      <div class="metric-grid"><div class="metric">Panel average<b>${p.average_total ?? '-'} / 100</b></div><div class="metric">Evaluators scored<b>${p.reviewers.length}</b></div><div class="metric">Spread<b>${p.spread}</b></div></div>
      <div class="table-wrap"><table class="table"><thead><tr><th>Criterion</th>${p.reviewers.map((r) => `<th>${esc(r.reviewer)}</th>`).join('')}<th>Average</th></tr></thead><tbody>
      ${crit.map((c) => `<tr><td>${esc(c.label)}</td>${p.reviewers.map((r) => `<td>${r.scores[c.id] ? `${r.scores[c.id].score}${r.scores[c.id].comment ? `<div class="small muted">${esc(r.scores[c.id].comment)}</div>` : ''}` : '-'}</td>`).join('')}<td><b>${p.criterion_averages[c.id] ?? '-'}</b></td></tr>`).join('')}
      <tr><td><b>Weighted total</b></td>${p.reviewers.map((r) => `<td><b>${r.total ?? '-'}</b>${r.complete ? '' : ' <span class="small muted">(partial)</span>'}</td>`).join('')}<td><b>${p.average_total ?? '-'}</b></td></tr></tbody></table></div>`
      : '<div class="empty">No scores recorded yet.</div>';
  } else if (!sc.can_score && sc.my_total === null) h += '<div class="empty">Scoring opens when the idea is in panel review and you are assigned to it.</div>';
  return h + '</div>';
}
async function pScoring() {
  const rows = await api('/api/score-criteria');
  const admin = me.role === 'admin';
  const total = rows.filter((c) => c.enabled).reduce((n, c) => n + c.weight, 0);
  $('page').innerHTML = `<div class="card"><h2>Scoring criteria</h2><p class="muted">Evaluators score each enabled criterion from 1 to 5. Weights are relative (enabled total: ${total}). ${admin ? 'Changes apply to future scoring.' : 'Only administrators can change criteria.'}</p>
    <div class="table-wrap"><table class="table"><thead><tr><th>Criterion</th><th>Weight</th><th>Enabled</th><th></th></tr></thead><tbody>${rows.map((c) => `<tr><td><b>${esc(c.label)}</b><div class="small muted">${esc(c.description)}</div></td>
    <td>${admin ? `<input data-f="weight" type="number" min="0.1" max="100" step="any" value="${esc(c.weight)}" style="width:80px">` : esc(c.weight)}</td>
    <td>${admin ? `<input data-f="enabled" type="checkbox" ${c.enabled ? 'checked' : ''}>` : c.enabled ? 'yes' : 'no'}</td>
    <td>${admin ? `<button class="small" data-action="score-crit-save" data-id="${esc(c.id)}">Save</button>` : ''}</td></tr>`).join('')}</tbody></table></div></div>
    ${admin ? `<div class="card"><h3>Add criterion</h3><form data-form="score-crit-add"><div class="inline-form"><div class="field"><label>Key (a-z, 0-9, _)</label><input name="key" required pattern="[a-z0-9_]{2,60}"></div>
    <div class="field"><label>Label</label><input name="label" required minlength="2"></div><div class="field"><label>Weight</label><input name="weight" type="number" min="0.1" max="100" step="any" value="10" required></div>
    <div class="field"><label>Description</label><input name="description"></div></div><button type="submit">Add criterion</button></form></div>` : ''}`;
}
async function pCommittee() {
  const d = await api('/api/applications?status=refer_committee&limit=100');
  $('page').innerHTML = `<div class="card"><h2>Adjudication queue (${d.total})</h2><div class="table-wrap"><table class="table"><thead><tr><th>Idea</th><th>Sector</th><th>Stage</th><th>Status</th><th>Screening coverage</th><th>Last update</th><th></th></tr></thead><tbody>${appRows(d.items)}</tbody></table></div></div>`;
}
async function pStandards() {
  const [rows, progs] = await Promise.all([api('/api/rubric'), api('/api/programmes')]);
  const admin = me.role === 'admin';
  const scope = (id) => (id ? esc((progs.find((p) => p.id === id) || {}).name || id) : 'All programmes');
  $('page').innerHTML = `<div class="card"><h2>Institution standards (rubric)</h2><p class="muted">Each criterion is checked on every assessment. Numeric criteria are calculated; evidence criteria are keyword checks that an officer must verify; qualitative criteria are officer judgement (AI may add a note). ${admin ? '' : 'Only administrators can change standards.'}</p>
    <div class="table-wrap"><table class="table"><thead><tr><th>Criterion</th><th>Type</th><th>Rule</th><th>Mandatory</th><th>Weight</th><th>Enabled</th><th></th></tr></thead><tbody>${rows.map((c) => `<tr data-id="${esc(c.id)}"><td><b>${esc(c.label)}</b><div class="small muted">${esc(c.key)} - ${scope(c.programme_id)}</div></td><td>${esc(c.kind)}</td>
      <td>${c.kind === 'numeric' ? `${esc(c.metric)} ${esc(c.comparator)} ${admin ? `<input data-f="threshold" type="number" step="any" value="${esc(c.threshold)}" style="width:90px;display:inline-block">` : esc(c.threshold)}` : c.kind === 'evidence' ? `keywords: ${esc(c.keywords.join(', '))}` : 'officer judgement'}</td>
      <td>${admin ? `<input data-f="mandatory" type="checkbox" ${c.mandatory ? 'checked' : ''}>` : c.mandatory ? 'yes' : 'no'}</td>
      <td>${admin && c.kind !== 'qualitative' ? `<input data-f="weight" type="number" min="0" max="10" step="any" value="${esc(c.weight)}" style="width:70px">` : esc(c.weight)}</td>
      <td>${admin ? `<input data-f="enabled" type="checkbox" ${c.enabled ? 'checked' : ''}>` : c.enabled ? 'yes' : 'no'}</td>
      <td>${admin ? `<button class="small" data-action="rubric-save" data-id="${esc(c.id)}">Save</button>` : ''}</td></tr>`).join('')}</tbody></table></div></div>
    ${admin ? `<div class="card"><h3>Add criterion</h3><form data-form="rubric-add"><div class="inline-form">
    <div class="field"><label>Key (a-z, 0-9, _)</label><input name="key" required pattern="[a-z0-9_]{2,60}"></div><div class="field"><label>Label</label><input name="label" required></div>
    <div class="field"><label>Type</label><select name="kind"><option value="numeric">numeric</option><option value="evidence">evidence (keywords)</option><option value="qualitative">qualitative</option></select></div>
    <div class="field"><label>Programme</label><select name="programme_id"><option value="">All programmes</option>${progs.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}</select></div>
    <div class="field"><label>Metric (numeric)</label><select name="metric"><option>margin</option><option>funding_to_contribution</option><option>owner_contribution_pct</option><option>revenue</option><option>expenses</option><option>profit</option><option>expense_to_revenue_ratio</option><option>funding</option><option>contribution</option></select></div>
    <div class="field"><label>Comparator</label><select name="comparator"><option>&gt;=</option><option>&lt;=</option><option>&gt;</option><option>&lt;</option></select></div>
    <div class="field"><label>Threshold</label><input name="threshold" type="number" step="any"></div><div class="field"><label>Keywords (comma separated)</label><input name="keywords"></div>
    <div class="field"><label>Weight (0 for qualitative)</label><input name="weight" type="number" min="0" max="10" step="any" value="1"></div><div class="field"><label>Description</label><input name="description"></div>
    <div class="field"><label><input name="mandatory" type="checkbox"> Mandatory</label></div></div><button type="submit">Add criterion</button></form></div>` : ''}`;
}
async function pProgrammes() {
  const rows = await api('/api/programmes');
  const admin = me.role === 'admin';
  const form = (p) => `<form data-form="programme" data-id="${esc(p ? p.id : '')}"><div class="inline-form">
    <div class="field"><label>Name</label><input name="name" required value="${esc(p ? p.name : '')}"></div><div class="field"><label>Code</label><input name="code" required value="${esc(p ? p.code : '')}"></div>
    <div class="field"><label>Min request</label><input name="min_request" type="number" min="0" step="any" value="${esc(p ? p.min_request : 0)}"></div>
    <div class="field"><label>Max request (0 = none)</label><input name="max_request" type="number" min="0" step="any" value="${esc(p ? p.max_request : 0)}"></div>
    <div class="field"><label>Min owner share of cost %</label><input name="min_owner_contribution_pct" type="number" min="0" max="100" step="any" value="${esc(p ? p.min_owner_contribution_pct : 10)}"></div>
    <div class="field"><label>Description</label><input name="description" value="${esc(p ? p.description : '')}"></div>
    <div class="field"><label><input name="enabled" type="checkbox" ${!p || p.enabled ? 'checked' : ''}> Enabled</label></div></div><button type="submit">${p ? 'Save' : 'Add programme'}</button></form>`;
  $('page').innerHTML = `<div class="card"><h2>Funding programmes</h2><p class="small muted">Owner share = owner contribution / (funding + owner contribution).</p>${rows.map((p) => `<div class="card">${admin ? form(p) : `<b>${esc(p.name)}</b> <span class="small muted">${esc(p.code)}</span><p>${esc(p.description)}</p><div class="small muted">Min ${money(p.min_request)} - Max ${p.max_request ? money(p.max_request) : 'none'} - min owner share ${p.min_owner_contribution_pct}%</div>`}</div>`).join('') || '<div class="empty">No programmes.</div>'}</div>
    ${admin ? `<div class="card"><h3>Add programme</h3>${form(null)}</div>` : ''}`;
}
async function pKnowledge() {
  const rows = await api('/api/knowledge');
  $('page').innerHTML = `<div class="card"><h2>Policy knowledge base</h2><p class="small muted">Active items are matched to each application and supplied to the AI as context (when AI is enabled).</p>${rows.map((p) => `<div class="finding"><b>${esc(p.title)}</b> ${pill('v' + p.version)} ${p.active ? pill('active', 'good') : pill('inactive')}<p>${esc(p.content)}</p></div>`).join('') || '<div class="empty">None.</div>'}</div>
    ${me.role === 'admin' ? `<div class="card"><h3>Add item</h3><form data-form="knowledge"><div class="field"><label>Title</label><input name="title" required></div><div class="field"><label>Content (min 20 characters)</label><textarea name="content" rows="4" required minlength="20"></textarea></div><button type="submit">Add</button></form></div>` : ''}`;
}
async function pManagement() {
  const [d, p] = await Promise.all([api('/api/management/dashboard'), api('/api/pilot/summary')]);
  const list = (o) => Object.entries(o).map(([k, v]) => `<div class="row between" style="padding:8px 0;border-bottom:1px solid var(--line)"><span>${esc(k.replace('_', ' '))}</span><b>${v}</b></div>`).join('') || '<div class="empty">No data.</div>';
  $('page').innerHTML = `<div class="grid"><div class="card"><div class="muted">Applications</div><div class="stat">${d.applications}</div></div><div class="card"><div class="muted">Funding requested</div><div class="stat">${money(d.funding_requested)}</div></div>
    <div class="card"><div class="muted">Open due diligence</div><div class="stat">${d.open_due_diligence}</div></div><div class="card"><div class="muted">Average rubric coverage</div><div class="stat">${pct(d.average_coverage_pct)}</div></div></div>
    <div class="grid"><div class="card"><h3>Status</h3>${list(d.status)}</div><div class="card"><h3>Risks flagged</h3>${list(d.risk_counts)}</div>
    <div class="card"><h3>Pilot KPIs</h3>${list({ 'assessments completed': p.assessments_completed, 'reviewed': p.reviewed, 'committee referrals': p.committee_referrals, 'final decisions': p.final_decisions, 'due diligence answered %': p.due_diligence_answer_rate, 'assessment completion %': p.pilot_metrics.assessment_completion_rate, 'decision coverage %': p.pilot_metrics.decision_coverage_rate })}</div></div>`;
}
async function pNotifications() {
  const rows = await api('/api/notifications');
  $('page').innerHTML = `<div class="card"><h2>Notifications</h2>${rows.map((n) => `<div class="finding" ${n.read ? 'style="opacity:.6"' : ''}><div class="row between"><span>${esc(n.message)}<div class="small muted">${esc(when(n.created_at))}</div></span><span class="row">${n.application_id ? `<button class="small ghost" data-action="open-app" data-id="${esc(n.application_id)}">Open</button>` : ''}${n.read ? '' : `<button class="small" data-action="notif-read" data-id="${n.id}">Mark read</button>`}</span></div></div>`).join('') || '<div class="empty">No notifications.</div>'}</div>`;
}
async function pUsers() {
  const rows = await api('/api/users');
  $('page').innerHTML = `<div class="card"><h2>Users</h2><div class="table-wrap"><table class="table"><thead><tr><th>Username</th><th>Role</th><th>Status</th><th>Created</th><th></th></tr></thead><tbody>${rows.map((u) => `<tr><td>${esc(u.username)}</td><td>${esc(roleLabel(u.role))}</td><td>${u.active ? pill('active', 'good') : pill('inactive', 'bad')}</td><td>${esc(when(u.created_at))}</td>
    <td>${u.id === me.id ? '' : `<button class="small ghost" data-action="user-toggle" data-id="${u.id}" data-active="${u.active ? 1 : 0}">${u.active ? 'Deactivate' : 'Activate'}</button> <button class="small ghost" data-action="user-reset" data-id="${u.id}">Reset password</button>`}</td></tr>`).join('')}</tbody></table></div></div>
    <div class="card"><h3>Create user</h3><form data-form="user-create"><div class="inline-form"><div class="field"><label>Username</label><input name="username" required minlength="3" pattern="[A-Za-z0-9_.\\-]+"></div>
    <div class="field"><label>Full name</label><input name="full_name" maxlength="200"></div>
    <div class="field"><label>Email</label><input name="email" type="email"></div><div class="field"><label>Mobile</label><input name="phone" type="tel"></div>
    <div class="field"><label>Temporary password (min 12)</label><input name="password" type="password" required minlength="12" autocomplete="new-password"></div>
    <div class="field"><label>Role</label><select name="role"><option value="officer">Evaluator</option><option value="committee">Adjudicator</option><option value="applicant">Submitter</option><option value="admin">Administrator</option></select></div></div>
    <p class="small muted">Evaluators score assigned ideas and refer them to adjudication. Adjudicators make the final decision and cannot decide an idea they referred. Submitters see only their own ideas.</p><button type="submit">Create user</button></form></div>`;
}
async function pAudit() {
  const rows = await api('/api/audit?limit=300');
  $('page').innerHTML = `<div class="card"><div class="row between"><h2>Audit trail</h2><div class="row"><span id="chain" class="small muted"></span><button class="ghost" data-action="audit-verify">Verify integrity</button></div></div>
    <div class="table-wrap"><table class="table"><thead><tr><th>Time</th><th>User</th><th>Action</th><th>Entity</th><th>Detail</th></tr></thead><tbody>${rows.map((x) => `<tr><td>${esc(when(x.created_at))}</td><td>${esc(x.user)}</td><td>${esc(x.action)}</td><td>${esc(x.entity_type)} / ${esc(String(x.entity_id).slice(0, 8))}</td><td class="small">${esc(x.detail)}</td></tr>`).join('')}</tbody></table></div></div>`;
}
async function pMessages() {
  const j = await api('/api/admin/messages?limit=200');
  const ch = (c) => (c === 'log' ? 'not configured - recorded here only' : c);
  const c = j.counts;
  $('page').innerHTML = `<div class="card"><h2>Message log</h2><p class="muted">Every email and SMS the portal sends: sign-up codes, password resets, sign-in codes and alerts. Codes are masked.</p>
    <div class="row">${pill('Email: ' + ch(j.channels.email))} ${pill('SMS: ' + ch(j.channels.sms))} ${pill(`Sent ${c.sent || 0}`, 'good')} ${pill(`Recorded ${c.logged || 0}`)} ${pill(`Waiting ${c.queued || 0}`, 'warn')} ${pill(`Failed ${c.failed || 0}`, 'bad')}</div>
    ${j.items.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Time</th><th>Channel</th><th>Type</th><th>To</th><th>Subject / message</th><th>Status</th></tr></thead><tbody>${j.items.map((m) => `<tr><td>${esc(when(m.created_at))}</td><td>${esc(m.channel)}</td><td>${esc(m.kind)}</td><td>${esc(m.to)}</td><td class="small"><b>${esc(m.subject || '')}</b> ${esc(m.body)}</td><td>${pill(m.status, { sent: 'good', failed: 'bad', queued: 'warn' }[m.status] || '')}${m.detail ? `<div class="small muted">${esc(m.detail)}</div>` : ''}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">No messages yet.</div>'}</div>`;
}
async function pSettings() {
  let ai = '';
  if (me.role === 'admin') {
    const i = await api('/api/institution');
    ai = `<div class="card"><h3>AI assistance</h3><p class="muted">When on, the business-plan text (e-mail addresses and phone numbers removed) is sent to the configured AI provider to draft commentary. Numbers and rubric results never depend on it. ${i.ai_configured ? `Provider configured (${esc(i.ai_model)}).` : '<b>No provider is configured on the server</b>, so rules only will be used.'}</p>
      <label><input id="aiToggle" type="checkbox" data-change="ai" ${i.ai_enabled ? 'checked' : ''}> Allow AI assistance for ${esc(i.name)}</label></div>`;
  }
  const p = await api('/api/me/preferences');
  const off = (on) => (on ? '' : ' <span class="small muted">(not set up on this server yet: messages are recorded in the log only)</span>');
  const contact = `<div class="card"><h3>Contact details and alerts</h3>
    <form data-form="prefs"><div class="inline-form"><div class="field"><label>Full name</label><input name="full_name" value="${esc(p.full_name || '')}" maxlength="200"></div>
    <div class="field"><label>Email ${p.email ? (p.email_verified ? pill('confirmed', 'good') : pill('not confirmed', 'warn')) : ''}</label><input name="email" type="email" value="${esc(p.email || '')}" autocomplete="email"></div>
    <div class="field"><label>Mobile number</label><input name="phone" type="tel" value="${esc(p.phone || '')}" placeholder="71 234 567" autocomplete="tel"></div></div>
    <p class="small muted">Free alerts tell you when an idea you follow changes status or needs something from you.</p>
    <label><input type="checkbox" name="alert_email" ${p.alert_email ? 'checked' : ''}> Email me alerts${off(p.channels.email)}</label><br>
    <label><input type="checkbox" name="alert_sms" ${p.alert_sms ? 'checked' : ''}> SMS me alerts${off(p.channels.sms)}</label><br>
    <label><input type="checkbox" name="otp_login" ${p.otp_login ? 'checked' : ''}> Ask for a one-time code from my email each time I sign in</label>
    <div><button type="submit">Save</button></div></form>
    ${p.pending_email ? `<form data-form="email-verify" class="inline-form"><div class="field"><label>Code sent to ${esc(p.pending_email)}</label><input name="code" inputmode="numeric" maxlength="6" required class="code-input"></div><button type="submit">Confirm new email</button></form>` : ''}</div>`;
  $('page').innerHTML = `${ai}${contact}<div class="card"><h3>Change password</h3><form data-form="pw"><div class="inline-form"><div class="field"><label>Current password</label><input name="current_password" type="password" required autocomplete="current-password"></div>
    <div class="field"><label>New password (min 12)</label><input name="new_password" type="password" required minlength="12" autocomplete="new-password"></div></div><button type="submit">Change password</button></form></div>`;
}

/* ---------- help, support, site content, application form ---------- */
const telLink = (p) => String(p || '').split(' / ').map((x) => (/^[+\d][\d\s]+$/.test(x.trim()) ? `<a href="tel:${x.replace(/[^\d+]/g, '')}">${esc(x)}</a>` : esc(x))).join(' / ');
function helpHTML(h, inPortal) {
  const c = h.content || {};
  const groups = {};
  h.faqs.forEach((f) => { (groups[f.category] = groups[f.category] || []).push(f); });
  const faqs = Object.entries(groups).map(([cat, items]) => `<h3>${esc(cat)}</h3>${items.map((f) => `<details class="faq"><summary>${esc(f.question)}</summary><p>${esc(f.answer)}</p></details>`).join('')}`).join('');
  const contacts = h.contacts.map((x) => `<div class="contact"><b>${esc(x.name)}</b><div class="small muted">${esc(x.purpose)}</div>
    <div class="small">${x.phone ? `Phone: ${telLink(x.phone)}<br>` : ''}${x.email ? `Email: <a href="mailto:${esc(x.email)}">${esc(x.email)}</a><br>` : ''}${x.website ? `<a href="${esc(x.website)}" target="_blank" rel="noopener">${esc(x.website.replace(/^https?:\/\//, ''))}</a><br>` : ''}${x.address ? esc(x.address) : ''}</div></div>`).join('');
  const reach = [c.support_email && `Email <a href="mailto:${esc(c.support_email)}">${esc(c.support_email)}</a>`, c.support_phone && `call ${telLink(c.support_phone)}`].filter(Boolean).join(' or ');
  return `<div class="card"><h2>Help and frequently asked questions</h2><p class="muted">${esc(c.support_intro)}</p>
    <p class="small"><b>Support hours:</b> ${esc(c.support_hours)}${reach ? ` &middot; ${reach}` : ''}</p>
    ${inPortal ? '<button data-action="nav" data-page="support">Ask a question</button>' : '<p class="small muted">Sign in to send a question to the support team.</p>'}</div>
    <div class="card">${faqs || '<div class="empty">No questions yet.</div>'}</div>
    <div class="card"><h2>Useful contacts</h2><p class="small muted">Regulators and partners you may need while turning an idea into a business.</p><div class="contacts">${contacts}</div></div>`;
}
async function pHelp() { await loadSite(); $('page').innerHTML = site ? helpHTML(site, true) : '<div class="card empty">Help is not available right now.</div>'; }

const TICKET_STATUS = { open: ['Waiting for reply', 'warn'], answered: ['Answered', 'good'], closed: ['Closed', ''] };
function ticketHTML(t, staff) {
  const st = TICKET_STATUS[t.status] || [t.status, ''];
  return `<div class="finding ${t.overdue ? 'high' : ''}"><div class="row between"><b>${esc(t.subject)}</b><span>${pill(st[0], st[1])} ${t.overdue ? pill('overdue', 'bad') : ''}</span></div>
    <div class="small muted">${esc(t.category_label)}${staff ? ` &middot; from ${esc(t.user)}` : ''} &middot; sent ${esc(when(t.created_at))} &middot; reply due by ${esc(when(t.due_at))}</div>
    <p>${esc(t.message)}</p>
    ${t.response ? `<div class="quote"><b>Reply${t.responded_by ? ' from ' + esc(t.responded_by) : ''}</b> <span class="small muted">${esc(when(t.responded_at))}</span><p>${esc(t.response)}</p></div>` : ''}
    ${staff && t.status !== 'closed' ? `<form data-form="ticket-reply" data-id="${esc(t.id)}"><div class="field"><label>Your reply</label><textarea name="response" rows="2" required minlength="2" maxlength="5000">${esc(t.response)}</textarea></div><button type="submit" class="small">${t.response ? 'Update reply' : 'Send reply'}</button> <button type="button" class="small ghost" data-action="ticket-close" data-id="${esc(t.id)}">Close</button></form>` : ''}
    ${!staff && t.status !== 'closed' ? `<button class="small ghost" data-action="ticket-close" data-id="${esc(t.id)}">${t.status === 'answered' ? 'That solved it - close' : 'Withdraw'}</button>` : ''}</div>`;
}
async function pSupport() {
  const [d] = await Promise.all([api('/api/support/tickets'), site ? null : loadSite()]);
  const staff = !!d.stats; const c = (site && site.content) || {};
  const days = (n) => (n ? `${n} working day${n === 1 ? '' : 's'}` : 'Same day');
  const times = `<div class="table-wrap"><table class="table"><thead><tr><th>What your question is about</th><th>We reply within</th></tr></thead><tbody>${d.categories.map((x) => `<tr><td>${esc(x.label)}</td><td>${esc(days(x.days))}</td></tr>`).join('')}</tbody></table></div>`;
  const stats = staff ? `<div class="grid"><div class="card"><div class="muted">Waiting for reply</div><div class="stat">${d.stats.open}</div></div><div class="card"><div class="muted">Overdue</div><div class="stat ${d.stats.overdue ? 'bad' : ''}">${d.stats.overdue}</div></div><div class="card"><div class="muted">Answered on time</div><div class="stat">${d.stats.answered ? Math.round((100 * d.stats.answered_on_time) / d.stats.answered) + '%' : '-'}</div></div></div>` : '';
  const form = `<div class="card"><h3>Ask a question</h3><form data-form="ticket"><div class="inline-form"><div class="field"><label>What is it about?</label><select name="category" required><option value="">Choose...</option>${d.categories.map((x) => `<option value="${esc(x.key)}">${esc(x.label)} (${esc(days(x.days))})</option>`).join('')}</select></div>
    <div class="field"><label>Subject</label><input name="subject" required minlength="3" maxlength="200"></div></div>
    <div class="field"><label>Your question</label><textarea name="message" rows="4" required minlength="10" maxlength="5000"></textarea></div><button type="submit">Send</button></form></div>`;
  $('page').innerHTML = `${stats}<div class="card"><h2>Support</h2><p class="muted">${esc(c.support_intro || '')}</p><p class="small"><b>Support hours:</b> ${esc(c.support_hours || '')}</p>${times}
    <p class="small"><a href="#" data-action="nav" data-page="help">Read the frequently asked questions</a></p></div>
    ${staff ? '' : form}<div class="card"><h3>${staff ? 'Support requests' : 'My questions'}</h3>${d.items.map((t) => ticketHTML(t, staff)).join('') || '<div class="empty">No questions yet.</div>'}</div>${staff ? form : ''}`;
}

async function pContent() {
  const [c, faqs, contacts] = await Promise.all([api('/api/admin/content'), api('/api/admin/faqs'), api('/api/admin/contacts')]);
  const cats = JSON.parse(c.support_categories || '[]').map((x) => `${x.key} | ${x.label} | ${x.days}`).join('\n');
  const t = (name, label, rows, help) => `<div class="field"><label>${label}</label><textarea name="${name}" rows="${rows}" maxlength="4000">${esc(c[name])}</textarea>${help ? `<div class="small muted">${help}</div>` : ''}</div>`;
  const faqForm = (f) => `<form data-form="faq" ${f ? `data-id="${esc(f.id)}"` : ''}><div class="inline-form"><div class="field"><label>Group</label><input name="category" required maxlength="80" value="${esc(f ? f.category : 'General')}"></div>
    <div class="field"><label>Order</label><input name="sort" type="number" min="0" max="10000" value="${f ? f.sort : 100}"></div></div>
    <div class="field"><label>Question</label><input name="question" required minlength="5" maxlength="300" value="${esc(f ? f.question : '')}"></div>
    <div class="field"><label>Answer</label><textarea name="answer" rows="3" required minlength="5" maxlength="5000">${esc(f ? f.answer : '')}</textarea></div>
    <label><input type="checkbox" name="published" ${!f || f.published ? 'checked' : ''}> Show on the help page</label>
    <div class="row"><button type="submit" class="small">${f ? 'Save' : 'Add question'}</button>${f ? `<button type="button" class="small ghost" data-action="faq-delete" data-id="${esc(f.id)}">Delete</button>` : ''}</div></form>`;
  const contactForm = (x) => `<form data-form="contact" ${x ? `data-id="${esc(x.id)}"` : ''}><div class="inline-form"><div class="field"><label>Organisation</label><input name="name" required minlength="2" maxlength="200" value="${esc(x ? x.name : '')}"></div>
    <div class="field"><label>Order</label><input name="sort" type="number" min="0" max="10000" value="${x ? x.sort : 100}"></div></div>
    <div class="field"><label>What they help with</label><input name="purpose" maxlength="1000" value="${esc(x ? x.purpose : '')}"></div>
    <div class="inline-form"><div class="field"><label>Phone</label><input name="phone" maxlength="120" value="${esc(x ? x.phone : '')}"></div><div class="field"><label>Email</label><input name="email" type="email" maxlength="200" value="${esc(x ? x.email : '')}"></div></div>
    <div class="inline-form"><div class="field"><label>Website</label><input name="website" type="url" maxlength="200" value="${esc(x ? x.website : '')}"></div><div class="field"><label>Address</label><input name="address" maxlength="300" value="${esc(x ? x.address : '')}"></div></div>
    <label><input type="checkbox" name="published" ${!x || x.published ? 'checked' : ''}> Show on the help page</label>
    <div class="row"><button type="submit" class="small">${x ? 'Save' : 'Add contact'}</button>${x ? `<button type="button" class="small ghost" data-action="contact-delete" data-id="${esc(x.id)}">Delete</button>` : ''}</div></form>`;
  $('page').innerHTML = `<div class="card"><h2>Site content</h2><p class="muted">Change the wording people see, without a new release. Changes show straight away and are recorded in the audit trail.</p>
    <form data-form="content">${t('announcement', 'Announcement (shown at the top of everyone’s dashboard; leave empty to hide)', 2)}${t('welcome', 'Welcome message for submitters', 3)}
    ${t('support_intro', 'Help page introduction', 3)}<div class="inline-form"><div class="field"><label>Support hours</label><input name="support_hours" maxlength="300" value="${esc(c.support_hours)}"></div>
    <div class="field"><label>Support email</label><input name="support_email" type="email" value="${esc(c.support_email)}"></div><div class="field"><label>Support phone</label><input name="support_phone" value="${esc(c.support_phone)}"></div></div>
    <div class="field"><label>Support categories and reply times</label><textarea name="support_categories" rows="6" class="mono">${esc(cats)}</textarea><div class="small muted">One per line: <code>key | label | working days to reply</code>. Use 0 for same day. Keys are short words like <code>account</code>.</div></div>
    <button type="submit">Save content</button></form></div>
    <div class="card"><h2>Frequently asked questions (${faqs.length})</h2>${faqs.map((f) => `<details class="faq"><summary>${f.published ? '' : pill('hidden') + ' '}${esc(f.category)}: ${esc(f.question)}</summary>${faqForm(f)}</details>`).join('')}
    <details class="faq"><summary><b>+ Add a question</b></summary>${faqForm(null)}</details></div>
    <div class="card"><h2>Contacts (${contacts.length})</h2><p class="small muted">Check phone numbers and addresses before going live: organisations change them from time to time.</p>${contacts.map((x) => `<details class="faq"><summary>${x.published ? '' : pill('hidden') + ' '}${esc(x.name)}</summary>${contactForm(x)}</details>`).join('')}
    <details class="faq"><summary><b>+ Add a contact</b></summary>${contactForm(null)}</details></div>`;
}

const KIND_LABEL = { text: 'Short answer', textarea: 'Long answer', number: 'Number', select: 'Choose one', yes_no: 'Yes / No', date: 'Date', url: 'Web link' };
async function pFormBuilder() {
  const rows = await api('/api/form-fields?include_disabled=true');
  const fieldForm = (f) => `<form data-form="field" ${f ? `data-id="${esc(f.id)}"` : ''}><div class="inline-form">
    <div class="field"><label>Question</label><input name="label" required minlength="2" maxlength="200" value="${esc(f ? f.label : '')}"></div>
    <div class="field"><label>Key ${f ? '(cannot change)' : '(short, e.g. team_size)'}</label><input name="key" required pattern="[a-z][a-z0-9_]*" maxlength="60" value="${esc(f ? f.key : '')}" ${f ? 'readonly' : ''}></div>
    <div class="field"><label>Type</label><select name="kind">${Object.entries(KIND_LABEL).map(([k, l]) => `<option value="${k}" ${f && f.kind === k ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
    <div class="field"><label>Order</label><input name="sort" type="number" min="0" max="10000" value="${f ? f.sort : 100}"></div></div>
    <div class="field"><label>Choices (for "Choose one": one per line)</label><textarea name="options" rows="3">${esc(f ? f.options.join('\n') : '')}</textarea></div>
    <div class="field"><label>Help text</label><input name="help" maxlength="1000" value="${esc(f ? f.help : '')}"></div>
    <label><input type="checkbox" name="required" ${f && f.required ? 'checked' : ''}> Required</label> &nbsp; <label><input type="checkbox" name="enabled" ${!f || f.enabled ? 'checked' : ''}> Shown on the form</label>
    <div><button type="submit" class="small">${f ? 'Save question' : 'Add question'}</button></div></form>`;
  $('page').innerHTML = `<div class="card"><h2>Application form</h2><p class="muted">Every idea form asks for the title, problem, solution, target users, sector and stage. Add your own questions here; they appear on the form straight away. Questions cannot be deleted, so answers already given are kept. Hide a question instead.</p>
    ${rows.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Order</th><th>Question</th><th>Type</th><th>Required</th><th>Shown</th></tr></thead><tbody>${rows.map((f) => `<tr><td>${f.sort}</td><td>${esc(f.label)}<div class="small muted">${esc(f.key)}</div></td><td>${esc(KIND_LABEL[f.kind] || f.kind)}${f.kind === 'select' ? `<div class="small muted">${esc(f.options.join(', '))}</div>` : ''}</td><td>${f.required ? 'Yes' : 'No'}</td><td>${f.enabled ? pill('shown', 'good') : pill('hidden')}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">No extra questions yet.</div>'}</div>
    ${rows.map((f) => `<details class="card faq"><summary>Edit: ${esc(f.label)}</summary>${fieldForm(f)}</details>`).join('')}
    <div class="card"><h3>Add a question</h3>${fieldForm(null)}</div>
    <div class="card"><h3>Preview of the extra questions</h3><p class="small muted">This is how submitters see them, below the standard fields.</p>${formFields.length ? extraFields(null) : '<div class="empty">Nothing to preview yet.</div>'}</div>`;
}

/* ---------- actions ---------- */
const formData = (f) => Object.fromEntries(new FormData(f).entries());
const numOrNull = (v) => (v === '' || v === undefined || v === null ? null : Number(v));
const actions = {
  menu: () => setMenu(!$('side').classList.contains('open')),
  'menu-close': () => setMenu(false),
  logout,
  nav: (t) => show(t.dataset.page),
  'open-app': (t) => openApp(t.dataset.id).catch((e) => toast(e.message)),
  'apps-page': (t) => { listState.offset = Math.max(0, listState.offset + Number(t.dataset.dir) * listState.limit); show('applications'); },
  upload: async (t) => {
    const f = $('file').files[0];
    if (!f) return toast('Choose a document first');
    const fd = new FormData(); fd.append('file', f);
    $('uploadMsg').textContent = 'Uploading, extracting and assessing...'; busy(t, true);
    try { await api(`/api/applications/${current.id}/analyse-file`, { method: 'POST', body: fd }); viewing = null; toast(isStaff() ? 'Document screened' : 'Submitted for review'); await show('review'); }
    catch (e) { $('uploadMsg').textContent = e.message; toast(e.message); } finally { busy(t, false); }
  },
  'clear-financials': async () => { try { await send(`/api/applications/${current.id}/financials`, { revenue: null, expenses: null }); toast('Overrides cleared'); show('review'); } catch (e) { toast(e.message); } },
  'dd-save': async (t) => {
    const id = t.dataset.id;
    try { await send(`/api/applications/${current.id}/due-diligence/${id}/answer`, { answer: $('dd_' + id).value, evidence_note: $('dde_' + id).value }); toast('Answer saved'); show('review'); } catch (e) { toast(e.message); }
  },
  decide: async (t) => {
    const rationale = ($('reason').value || '').trim();
    if (rationale.length < 10) return toast('Please enter a rationale of at least 10 characters.');
    if (!confirm(`Record "${DECISION_LABEL[t.dataset.decision]}" for "${current.idea_title || current.applicant_name}"? This is recorded in the audit log.`)) return;
    busy(t, true);
    try { await send(`/api/applications/${current.id}/decision`, { decision: t.dataset.decision, rationale, conditions: $('conditions').value }); toast('Decision recorded'); show('review'); }
    catch (e) { toast(e.message); busy(t, false); }
  },
  'report-html': () => openReport('html'),
  'report-docx': () => openReport('docx'),
  'knowledge-context': async () => { try { const j = await api(`/api/applications/${current.id}/knowledge-context`, { method: 'POST' }); toast(`${j.items.length} relevant policy item(s): ${j.items.map((i) => i.title).join('; ') || 'none'}`); } catch (e) { toast(e.message); } },
  'latest-version': () => { viewing = null; show('review'); },
  'rubric-save': async (t) => {
    const tr = t.closest('tr'); const rows = await api('/api/rubric'); const c = rows.find((x) => x.id === t.dataset.id);
    const g = (f) => tr.querySelector(`[data-f="${f}"]`);
    const body = { ...c, threshold: g('threshold') ? numOrNull(g('threshold').value) : c.threshold, mandatory: g('mandatory').checked, enabled: g('enabled').checked, weight: g('weight') ? Number(g('weight').value) : c.weight };
    delete body.id;
    try { await send('/api/rubric/' + c.id, body, 'PUT'); toast('Criterion saved'); show('standards'); } catch (e) { toast(e.message); }
  },
  'score-crit-save': async (t) => {
    const tr = t.closest('tr'); const rows = await api('/api/score-criteria'); const c = rows.find((x) => x.id === t.dataset.id);
    const body = { key: c.key, label: c.label, description: c.description, weight: Number(tr.querySelector('[data-f="weight"]').value), enabled: tr.querySelector('[data-f="enabled"]').checked };
    try { await send('/api/score-criteria/' + c.id, body, 'PUT'); toast('Criterion saved'); show('scoring'); } catch (e) { toast(e.message); }
  },
  'user-toggle': async (t) => { try { await send('/api/users/' + t.dataset.id, { active: t.dataset.active !== '1' }, 'PATCH'); show('users'); } catch (e) { toast(e.message); } },
  'user-reset': async (t) => {
    const pw = prompt('New temporary password (at least 12 characters):'); if (!pw) return;
    try { await send('/api/users/' + t.dataset.id, { password: pw }, 'PATCH'); toast('Password reset'); } catch (e) { toast(e.message); }
  },
  'notif-read': async (t) => { try { await api(`/api/notifications/${t.dataset.id}/read`, { method: 'POST' }); show('notifications'); } catch (e) { toast(e.message); } },
  'ticket-close': async (t) => { if (!confirm('Close this request?')) return; await api(`/api/support/tickets/${t.dataset.id}/close`, { method: 'POST' }); toast('Closed'); show('support'); },
  'faq-delete': async (t) => { if (!confirm('Delete this question for good?')) return; await api('/api/faqs/' + t.dataset.id, { method: 'DELETE' }); toast('Deleted'); show('content'); },
  'contact-delete': async (t) => { if (!confirm('Delete this contact for good?')) return; await api('/api/contacts/' + t.dataset.id, { method: 'DELETE' }); toast('Deleted'); show('content'); },
  'audit-verify': async () => { try { const j = await api('/api/audit/verify'); $('chain').textContent = j.ok ? `Chain intact (${j.checked} records)` : `TAMPERING DETECTED at record ${j.first_bad_id}`; $('chain').className = 'small ' + (j.ok ? '' : 'bad'); } catch (e) { toast(e.message); } },
};
const forms = {
  'create-app': async (f) => {
    const d = formData(f);
    const j = await send('/api/applications', { applicant_name: d.applicant_name, funding_request: Number(d.funding_request || 0), owner_contribution: Number(d.owner_contribution || 0), programme_id: d.programme_id || null,
      idea_title: d.idea_title, problem: d.problem, solution: d.solution, target_users: d.target_users, sector: d.sector, stage: d.stage, answers: answersFrom(d) });
    toast('Idea saved - now upload your document to submit'); await openApp(j.id);
  },
  'idea-edit': async (f) => {
    const d = formData(f);
    await send(`/api/applications/${current.id}/idea`, { idea_title: d.idea_title, problem: d.problem, solution: d.solution, target_users: d.target_users, sector: d.sector, stage: d.stage, answers: answersFrom(d) }, 'PUT');
    toast('Idea updated'); show('review');
  },
  assign: async (f) => {
    const ids = [...f.querySelectorAll('input[name=r]:checked')].map((x) => Number(x.value));
    await send(`/api/applications/${current.id}/assignments`, { reviewer_ids: ids }, 'PUT'); toast('Evaluators assigned'); show('review');
  },
  scores: async (f) => {
    const d = formData(f);
    const scores = Object.keys(d).filter((k) => k.startsWith('s_') && d[k]).map((k) => ({ criterion_id: k.slice(2), score: Number(d[k]), comment: d['c_' + k.slice(2)] || '' }));
    if (!scores.length) throw new Error('Choose at least one score.');
    await send(`/api/applications/${current.id}/scores`, { scores }, 'PUT'); toast('Scores saved'); show('review');
  },
  'score-crit-add': async (f) => {
    const d = formData(f);
    await send('/api/score-criteria', { key: d.key, label: d.label, description: d.description || '', weight: Number(d.weight), enabled: true }); toast('Criterion added'); show('scoring');
  },
  financials: async (f) => { const d = formData(f); await send(`/api/applications/${current.id}/financials`, { revenue: numOrNull(d.revenue), expenses: numOrNull(d.expenses) }); toast('Figures confirmed - new assessment version created'); show('review'); },
  claim: async (f) => { await send(`/api/applications/${current.id}/claim-evidence`, formData(f)); toast('Evidence recorded'); show('review'); },
  'rubric-add': async (f) => {
    const d = formData(f);
    const body = { key: d.key, label: d.label, description: d.description || '', kind: d.kind, programme_id: d.programme_id || null, mandatory: !!d.mandatory, weight: d.kind === 'qualitative' ? 0 : Number(d.weight || 1), enabled: true,
      metric: d.kind === 'numeric' ? d.metric : null, comparator: d.kind === 'numeric' ? d.comparator : null, threshold: d.kind === 'numeric' ? numOrNull(d.threshold) : null,
      keywords: d.kind === 'evidence' ? (d.keywords || '').split(',').map((x) => x.trim()).filter(Boolean) : [] };
    await send('/api/rubric', body); toast('Criterion added'); show('standards');
  },
  programme: async (f) => {
    const d = formData(f); const body = { name: d.name, code: d.code, description: d.description || '', min_request: Number(d.min_request || 0), max_request: Number(d.max_request || 0), min_owner_contribution_pct: Number(d.min_owner_contribution_pct || 0), enabled: !!d.enabled };
    await send(f.dataset.id ? '/api/programmes/' + f.dataset.id : '/api/programmes', body, f.dataset.id ? 'PUT' : 'POST'); toast('Programme saved'); show('programmes');
  },
  knowledge: async (f) => { await send('/api/knowledge', { ...formData(f), category: 'policy', version: '1.0', active: true }); toast('Item added'); show('knowledge'); },
  'user-create': async (f) => { await send('/api/users', formData(f)); toast('User created'); show('users'); },
  prefs: async (f) => {
    const d = formData(f);
    const j = await send('/api/me/preferences', { full_name: d.full_name, email: d.email, phone: d.phone, alert_email: !!d.alert_email, alert_sms: !!d.alert_sms, otp_login: !!d.otp_login }, 'PUT');
    toast(j.sent_to ? `Saved. We sent a code to ${j.sent_to} to confirm the new address.${j.demo_code ? ' Demo code: ' + j.demo_code : ''}` : 'Saved'); show('settings');
  },
  'email-verify': async (f) => { await send('/api/me/email/verify', formData(f)); toast('Email address confirmed'); show('settings'); },
  ticket: async (f) => { await send('/api/support/tickets', formData(f)); toast('Sent. We will reply within the time shown.'); show('support'); },
  'ticket-reply': async (f) => { await send(`/api/support/tickets/${f.dataset.id}/reply`, formData(f)); toast('Reply sent'); show('support'); },
  content: async (f) => {
    const d = formData(f);
    const cats = d.support_categories.split('\n').map((l) => l.trim()).filter(Boolean).map((l) => {
      const p = l.split('|').map((x) => x.trim());
      if (p.length !== 3 || !/^\d+$/.test(p[2])) throw new Error(`Check this line: "${l}". Use key | label | days.`);
      return { key: p[0], label: p[1], days: Number(p[2]) };
    });
    await send('/api/admin/content', { ...d, support_categories: cats }, 'PUT'); await loadSite(); toast('Content saved'); show('content');
  },
  faq: async (f) => {
    const d = formData(f); const body = { category: d.category, question: d.question, answer: d.answer, sort: Number(d.sort || 100), published: !!d.published };
    await send(f.dataset.id ? '/api/faqs/' + f.dataset.id : '/api/faqs', body, f.dataset.id ? 'PUT' : 'POST'); toast('Saved'); show('content');
  },
  contact: async (f) => {
    const d = formData(f); const body = { ...d, sort: Number(d.sort || 100), published: !!d.published };
    await send(f.dataset.id ? '/api/contacts/' + f.dataset.id : '/api/contacts', body, f.dataset.id ? 'PUT' : 'POST'); toast('Saved'); show('content');
  },
  field: async (f) => {
    const d = formData(f);
    const body = { key: d.key, label: d.label, help: d.help || '', kind: d.kind, options: (d.options || '').split('\n').map((x) => x.trim()).filter(Boolean), required: !!d.required, enabled: !!d.enabled, sort: Number(d.sort || 100) };
    await send(f.dataset.id ? '/api/form-fields/' + f.dataset.id : '/api/form-fields', body, f.dataset.id ? 'PUT' : 'POST'); await loadFormFields(); toast('Question saved'); show('formbuilder');
  },
  pw: async (f) => { const j = await send('/api/me/password', formData(f)); token = j.access_token; sessionStorage.setItem('fl_token', token); toast('Password changed'); f.reset(); },
};
const changes = {
  filter: (t) => { listState.status = t.value; listState.offset = 0; show('applications'); },
  version: (t) => { viewing = Number(t.value); show('review'); },
  ai: async (t) => { try { await send('/api/institution', { ai_enabled: t.checked }, 'PUT'); toast(t.checked ? 'AI assistance enabled' : 'AI assistance disabled'); } catch (e) { t.checked = !t.checked; toast(e.message); } },
};
document.addEventListener('click', (e) => { const t = e.target.closest('[data-action]'); if (t && actions[t.dataset.action]) { e.preventDefault(); Promise.resolve(actions[t.dataset.action](t, e)).catch((err) => toast(err.message)); } });
document.addEventListener('change', (e) => { const t = e.target.closest('[data-change]'); if (t && changes[t.dataset.change]) changes[t.dataset.change](t); });
document.addEventListener('submit', (e) => {
  const f = e.target.closest('[data-form]'); if (!f || !forms[f.dataset.form]) return;
  e.preventDefault(); const btn = f.querySelector('button[type=submit]'); busy(btn, true);
  Promise.resolve(forms[f.dataset.form](f)).catch((err) => toast(err.message)).finally(() => busy(btn, false));
});
/* ---------- sign-in, sign-up, codes ---------- */
const auth = { who: '' };
const authMsg = (ok, text) => {
  $('loginError').classList.add('hidden'); $('loginOk').classList.add('hidden');
  if (text) { const el = ok ? $('loginOk') : $('loginError'); el.innerHTML = text; el.classList.remove('hidden'); }
};
const sentLine = (j, what) => `${what}${j.sent_to ? ` We sent a 6-digit code to <b>${esc(j.sent_to)}</b>.` : ''}${j.demo_code ? ` <br>Demo mode - your code is <span class="demo-code">${esc(j.demo_code)}</span>` : ''}`;
const codeField = (label) => `<div class="field"><label>${label}</label><input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{6}" required class="code-input"></div>`;
const back = '<p class="auth-links"><a href="#" data-auth="signin">Back to sign in</a></p>';
const AUTH_VIEWS = {
  register: () => `<h3>Create an account</h3><p class="small muted">For anyone who wants to submit an innovative idea to CEDA.</p><form data-auth-form="register">
    <div class="field"><label>Full name</label><input name="full_name" required minlength="2" maxlength="200" autocomplete="name"></div>
    <div class="field"><label>Username</label><input name="username" required minlength="3" maxlength="60" pattern="[A-Za-z0-9_.\-]+" autocomplete="username"></div>
    <div class="field"><label>Email</label><input name="email" type="email" required autocomplete="email"></div>
    <div class="field"><label>Mobile number (optional, for SMS alerts)</label><input name="phone" type="tel" placeholder="71 234 567" autocomplete="tel"></div>
    <div class="field"><label>Password (at least 12 characters)</label><input name="password" type="password" required minlength="12" autocomplete="new-password"></div>
    <label class="small"><input type="checkbox" name="accept_privacy" required> I agree that CEDA may use these details to process my idea and contact me about it.</label>
    <button class="wide" type="submit">Create account</button></form>${back}`,
  verify: () => `<h3>Confirm your email</h3><form data-auth-form="verify">${codeField('Code from the email')}<button class="wide" type="submit">Confirm and sign in</button></form>
    <p class="auth-links"><a href="#" data-auth="resend">Send a new code</a><a href="#" data-auth="signin">Back to sign in</a></p>`,
  forgot: () => `<h3>Reset your password</h3><p class="small muted">Enter your username or email. If we find the account we will send a reset code.</p><form data-auth-form="forgot">
    <div class="field"><label>Username or email</label><input name="identifier" required autocomplete="username" value="${esc(auth.who)}"></div><button class="wide" type="submit">Send reset code</button></form>${back}`,
  reset: () => `<h3>Choose a new password</h3><form data-auth-form="reset">${codeField('Reset code')}
    <div class="field"><label>New password (at least 12 characters)</label><input name="new_password" type="password" required minlength="12" autocomplete="new-password"></div><button class="wide" type="submit">Save new password</button></form>${back}`,
  help: () => '<div class="empty">Loading...</div>',
  otp: () => `<h3>Enter your sign-in code</h3><form data-auth-form="otp">${codeField('Code from the email')}<button class="wide" type="submit">Sign in</button></form>${back}`,
};
function authView(name, msg, ok = true) {
  const alt = $('authAlt');
  document.querySelector('.login-card').classList.remove('wide-card');
  if (name === 'signin') { alt.classList.add('hidden'); alt.innerHTML = ''; $('loginForm').classList.remove('hidden'); }
  else { $('loginForm').classList.add('hidden'); alt.innerHTML = AUTH_VIEWS[name](); alt.classList.remove('hidden'); const i = alt.querySelector('input'); if (i) i.focus(); }
  authMsg(ok, msg || '');
}
function signedIn(d) { token = d.access_token; sessionStorage.setItem('fl_token', token); $('password').value = ''; authView('signin'); boot(); }
async function authPost(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(errText(d, r.status)), { data: d });
  return d;
}
const AUTH_FORMS = {
  register: async (d) => { const j = await authPost('/api/auth/register', { ...d, accept_privacy: !!d.accept_privacy }); auth.who = j.username; authView('verify', sentLine(j, 'Account created.')); },
  verify: async (d) => signedIn(await authPost('/api/auth/verify', { identifier: auth.who, code: d.code })),
  forgot: async (d) => { auth.who = d.identifier.trim(); const j = await authPost('/api/auth/forgot', { identifier: auth.who }); authView('reset', sentLine(j, 'If that account exists, a reset code is on its way.')); },
  reset: async (d) => { await authPost('/api/auth/reset', { identifier: auth.who, code: d.code, new_password: d.new_password }); $('username').value = auth.who; authView('signin', 'Password changed. Please sign in with your new password.'); },
  otp: async (d) => signedIn(await authPost('/api/auth/login-code', { identifier: auth.who, code: d.code })),
};
document.addEventListener('click', async (e) => {
  const a = e.target.closest('[data-auth]'); if (!a) return; e.preventDefault();
  if (a.dataset.auth === 'resend') {
    try { const j = await authPost('/api/auth/resend', { identifier: auth.who }); authMsg(true, sentLine(j, 'If the account still needs confirming, a new code is on its way.')); } catch (ex) { authMsg(false, esc(ex.message)); }
    return;
  }
  if (a.dataset.auth === 'forgot') auth.who = $('username').value.trim();
  authView(a.dataset.auth);
  if (a.dataset.auth === 'help') {
    await loadSite();
    $('authAlt').innerHTML = (site ? helpHTML(site, false) : '<div class="empty">Help is not available right now.</div>') + back;
    document.querySelector('.login-card').classList.add('wide-card');
  }
});
document.addEventListener('submit', async (e) => {
  const f = e.target.closest('[data-auth-form]'); if (!f) return; e.preventDefault();
  const btn = f.querySelector('button[type=submit]'); busy(btn, true);
  try { await AUTH_FORMS[f.dataset.authForm](formData(f)); } catch (ex) { authMsg(false, esc(ex.message)); } finally { busy(btn, false); }
});
$('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault(); authMsg(true, '');
  const body = new URLSearchParams(); body.set('username', $('username').value.trim()); body.set('password', $('password').value);
  try {
    const r = await fetch('/api/token', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body });
    const d = await r.json().catch(() => ({}));
    if (r.status === 403 && d.verification_required) { auth.who = d.username; authView('verify', sentLine(d, 'Please confirm your email address before signing in.')); return; }
    if (!r.ok) throw new Error(errText(d, r.status));
    if (d.otp_required) { auth.who = d.username; authView('otp', sentLine(d, 'One more step.')); return; }
    signedIn(d);
  } catch (ex) { authMsg(false, esc(ex.message)); }
});
fetch('/api/auth/options').then((r) => r.json()).then((o) => { if (!o.self_registration) $('regLink').classList.add('hidden'); }).catch(() => {});
boot();

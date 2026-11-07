/* Seismic Response GUI.
 *
 * Talks to the Python package through a tiny JSON API (see seismic_response/web_api.py).
 * Two interchangeable backends are used:
 *   - "http":     the local `seismic-response gui` server (instant, works offline)
 *   - "pyodide":  the same Python running in this page via WebAssembly (no server needed)
 */
'use strict';

const PYODIDE_URL = 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.js';
const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ backend */

const backend = {
  kind: null,
  api: null,

  async init() {
    try {
      const r = await fetch('api/ping', { cache: 'no-store' });
      if (r.ok && (await r.json()).ok) { this.kind = 'http'; return; }
    } catch (e) { /* no local server: fall through to Pyodide */ }
    this.kind = 'pyodide';
    setBadge('loading Python (WebAssembly)…');
    await loadScript(PYODIDE_URL);
    const pyodide = await loadPyodide();
    const buf = await (await fetch('seismic_response.zip')).arrayBuffer();
    pyodide.unpackArchive(buf, 'zip', { extractDir: '/pkg' });
    pyodide.runPython("import sys\nif '/pkg' not in sys.path: sys.path.insert(0, '/pkg')");
    this.api = pyodide.pyimport('seismic_response.web_api');
  },

  async call(fn, ...args) {
    if (this.kind === 'http') {
      let r;
      try {
        r = await fetch('api/' + fn, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(args),
        });
      } catch (e) { // network failure: the local server has gone away
        setBadge('local Python: not responding');
        throw Object.assign(new Error('The local seismic-response server is not responding. Restart it and load the responses again.'), { offline: true });
      }
      if (!r.ok) throw new Error('Server error ' + r.status);
      setBadge('local Python', true);
      return JSON.parse(await r.text());
    }
    return JSON.parse(this.api.call(fn, ...args));
  },
};

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src; s.onload = resolve; s.onerror = () => reject(new Error('Could not load ' + src));
    document.head.appendChild(s);
  });
}

function setBadge(text, ok = false) {
  const b = $('backend');
  b.textContent = text;
  b.classList.toggle('ok', ok);
}

/* ------------------------------------------------------------------ state */

const S = {
  channels: [],       // every loaded channel epoch (from the backend)
  cur: null,          // current channel epoch id (the current epoch is always plotted)
  checked: new Set(), // ids of the channel epochs ticked for comparison
  expanded: new Set(), // keys of the source groups (see sourceGroups()) shown expanded
  results: [],        // plotted evaluations: { chanId, label, color, current, res }
  last: null,         // current epoch's evaluation result
  request: null,      // request that produced it
  seq: 0,             // evaluation sequence number (to drop stale replies)
  excluded: new Set(), // stage numbers unchecked in the stage table
  freqTouched: false, // true once the user edits the frequency range by hand
};

const STAGE_COLORS = ['--c2', '--c3', '--c4', '--c5', '--c6', '--c7', '--c8'];
function stageColor(seq) { return STAGE_COLORS[(((seq - 1) % STAGE_COLORS.length) + STAGE_COLORS.length) % STAGE_COLORS.length]; }
const PLOT_COLORS = ['--c1', ...STAGE_COLORS];
function plotColor(i) { return PLOT_COLORS[i % PLOT_COLORS.length]; }

function minOf(a) { let m = Infinity; for (const v of a) if (v < m) m = v; return m; }
function maxOf(a) { let m = -Infinity; for (const v of a) if (v > m) m = v; return m; }

/* ------------------------------------------------------------------ helpers */

function globToRegExp(glob) {
  const esc = glob.replace(/[.+^${}()|\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp('^' + esc + '$', 'i');
}

function fmtDate(iso) { return iso ? iso.replace('T', ' ').replace('Z', '') : 'open'; }

function fmtMinute(iso) { return iso ? fmtDate(iso).slice(0, 16) : 'open'; } // 'YYYY-MM-DD hh:mm'

/** Filename-safe UTC time, 'YYYYMMDDThhmmssZ' (fractional seconds dropped). */
function fileStamp(iso) { return iso.replace(/\.\d+/, '').replace(/[-:]/g, ''); }

function fmtNum(v, digits = 4) {
  if (v === null || v === undefined || !isFinite(v)) return '–';
  if (v === 0) return '0';
  const a = Math.abs(v);
  return (a >= 1e5 || a < 1e-3) ? v.toExponential(digits - 1) : String(Number(v.toPrecision(digits)));
}

function showAlert(kind, text) {
  const div = document.createElement('div');
  div.className = 'alert ' + kind;
  div.textContent = text;
  $('alerts').appendChild(div);
}

function setMsg(el, text, kind = '') { el.textContent = text; el.className = 'msg ' + kind; }

function download(filename, blob) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

async function decodeText(file) {
  const buf = await file.arrayBuffer();
  try { return new TextDecoder('utf-8', { fatal: true }).decode(buf); }
  catch (e) { return new TextDecoder('windows-1252').decode(buf); } // e.g. ISO-8859-1 StationXML
}

/* ------------------------------------------------------------------ loading responses */

/** Load each source ({ name, text(): Promise<string> }) into the backend and report the outcome. */
async function loadSources(sources) {
  const msg = $('loadMsg');
  let added = 0;
  const problems = [];
  for (const s of sources) {
    try {
      const res = await backend.call('load', s.name, await s.text());
      if (res.ok) added += res.added; else problems.push(s.name + ': ' + res.error);
    } catch (e) { problems.push(s.name + ': ' + e.message); }
  }
  if (problems.length) setMsg(msg, problems.join('\n'), 'error');
  else setMsg(msg, `Loaded ${added} channel epoch${added === 1 ? '' : 's'}.`, 'ok');
  if (added) await refreshChannels();
}

function loadFiles(files) {
  return loadSources(files.map((f) => ({ name: f.name, text: () => decodeText(f) })));
}

/** Fetch response files from URLs (relative URLs resolve against the page). */
function loadUrls(urls) {
  const sources = [];
  const bad = [];
  for (const u of urls) {
    let href;
    try { href = new URL(u, location.href).href; } catch (e) { bad.push(u + ': not a valid URL'); continue; }
    sources.push({
      name: href,
      text: async () => {
        let r;
        try { r = await fetch(href); }
        catch (e) { throw new Error('request failed (the server may not allow cross-origin access)'); }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return decodeText(r);
      },
    });
  }
  if (!sources.length) { setMsg($('loadMsg'), bad.join('\n'), 'error'); return Promise.resolve(); }
  setMsg($('loadMsg'), 'Fetching…');
  return loadSources(sources).then(() => {
    if (bad.length) setMsg($('loadMsg'), $('loadMsg').textContent + '\n' + bad.join('\n'), 'error');
  });
}

function loadUrlField() {
  const url = $('urlInput').value.trim();
  if (url) loadUrls([url]);
}

async function clearResponses() {
  try { await backend.call('clear'); }
  catch (e) { setMsg($('loadMsg'), 'Could not clear: ' + e.message, 'error'); return; }
  $('clearBtn').disabled = true;
  S.seq++; // drop any evaluation still in flight
  S.channels = []; S.cur = null; S.last = null; S.request = null; S.results = [];
  S.checked.clear();
  S.expanded.clear();
  S.excluded.clear();
  $('alerts').innerHTML = '';
  setMsg($('loadMsg'), 'Cleared all loaded responses.', 'ok');
  renderSidList();
  drawEmpty();
}

async function fetchFdsn() {
  const msg = $('loadMsg');
  const q = new URLSearchParams();
  const val = (id, blank) => $(id).value.trim() || blank;
  q.set('net', val('fNet', '*'));
  q.set('sta', val('fSta', '*'));
  const loc = $('fLoc').value.trim();
  q.set('loc', loc === '' ? '--' : loc);
  q.set('cha', val('fCha', '*'));
  q.set('level', 'response');
  q.set('format', 'xml');
  q.set('nodata', '404');
  const url = $('svcUrl').value.trim() + '?' + q.toString();
  setMsg(msg, 'Fetching…');
  try {
    const r = await fetch(url);
    if (r.status === 404 || r.status === 204) throw new Error('The service has no matching response.');
    if (!r.ok) throw new Error('Service returned HTTP ' + r.status);
    const text = await r.text();
    const res = await backend.call('load', url, text);
    if (!res.ok) throw new Error(res.error);
    setMsg(msg, `Fetched ${res.added} channel epoch${res.added === 1 ? '' : 's'}.`, 'ok');
    await refreshChannels();
  } catch (e) {
    const cors = e instanceof TypeError ? ' (the request was blocked; the service may not allow cross-origin access)' : '';
    setMsg(msg, 'Fetch failed: ' + e.message + cors, 'error');
  }
}

/* ------------------------------------------------------------------ channel & epoch selection */

async function refreshChannels() {
  S.channels = await backend.call('channels');
  $('clearBtn').disabled = !S.channels.length;
  renderSidList();
}

function groupKey(c) { return `${c.load}|${c.source_id}`; }

/** One group per (load, Source ID); a Source ID present in several loads is numbered #1, #2, ... */
function sourceGroups() {
  const byKey = new Map();
  for (const c of S.channels) {
    const key = groupKey(c);
    if (!byKey.has(key)) byKey.set(key, { key, sid: c.source_id, load: c.load, origin: c.origin, label: c.source_id });
  }
  const groups = [...byKey.values()].sort((a, b) => (a.sid < b.sid ? -1 : a.sid > b.sid ? 1 : a.load - b.load));
  const total = new Map();
  for (const g of groups) total.set(g.sid, (total.get(g.sid) || 0) + 1);
  const index = new Map();
  for (const g of groups) {
    if (total.get(g.sid) < 2) continue;
    index.set(g.sid, (index.get(g.sid) || 0) + 1);
    g.label = `${g.sid} #${index.get(g.sid)}`;
  }
  return groups;
}

function groupOf(key) { return sourceGroups().find((g) => g.key === key) || null; }

function chanOf(id) { return S.channels.find((c) => c.id === id) || null; }

function epochsOf(g) {
  return S.channels.filter((c) => c.source_id === g.sid && c.load === g.load)
    .sort((a, b) => (a.start || '').localeCompare(b.start || ''));
}

function visibleGroups() {
  const filter = $('sidFilter').value.trim();
  const re = filter ? globToRegExp(filter.includes('*') || filter.includes('?') ? filter : '*' + filter + '*') : null;
  return sourceGroups().filter((g) => !re || re.test(g.sid));
}

/** Start of an epoch for labels: the date, plus hh:mm when not at midnight. */
function epochTag(c) {
  if (!c.start) return `→ ${fmtMinute(c.end).slice(0, 10)}`;
  const [date, time] = fmtMinute(c.start).split(' ');
  return time === '00:00' ? date : `${date} ${time}`;
}

/** Plot label of an epoch: the group label, plus the epoch start when the group has several epochs. */
function epochLabel(g, c) { return epochsOf(g).length > 1 ? `${g.label} · ${epochTag(c)}` : g.label; }

/** The current epoch first (it takes the first plot colour), then the visible ticked epochs in list order. */
function plottedEpochs() {
  const out = [];
  const cur = chanOf(S.cur);
  const groups = visibleGroups();
  const curGroup = cur ? groups.find((g) => g.key === groupKey(cur)) : null;
  if (curGroup) out.push({ chan: cur, group: curGroup, label: epochLabel(curGroup, cur) });
  for (const g of groups) {
    for (const c of epochsOf(g)) if (c.id !== S.cur && S.checked.has(c.id)) out.push({ chan: c, group: g, label: epochLabel(g, c) });
  }
  return out;
}

/** Tooltip for a group row: the distinct instrument descriptions of its epochs, then the source. */
function groupTip(g) {
  const instruments = [...new Set(epochsOf(g).map((c) => c.instrument).filter(Boolean))];
  return [...instruments.map((d) => `Instrument: ${d}`), `Source: ${g.origin}`].join('\n');
}

function epochTip(c) {
  return [`${fmtDate(c.start)} → ${fmtDate(c.end)}`, ...(c.instrument ? [`Instrument: ${c.instrument}`] : [])].join('\n');
}

function renderRows() {
  const list = $('sidList');
  const groups = visibleGroups();
  const plotted = plottedEpochs();
  const ticked = (c) => S.checked.has(c.id);
  const active = list.contains(document.activeElement)
    ? { id: document.activeElement.dataset.id, box: document.activeElement.tagName === 'INPUT' } : null;
  list.innerHTML = '';
  for (const g of groups) {
    const eps = epochsOf(g);
    const open = S.expanded.has(g.key);
    const on = eps.filter(ticked).length;
    const row = document.createElement('div');
    row.className = 'sid-row group';
    row.dataset.id = 'g:' + g.key;
    const chev = document.createElement('span');
    chev.className = 'sid-chevron'; chev.setAttribute('aria-hidden', 'true'); chev.textContent = open ? '▾' : '▸';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = on === eps.length; cb.indeterminate = on > 0 && on < eps.length;
    cb.dataset.id = row.dataset.id;
    cb.setAttribute('aria-label', `Compare all epochs of ${g.label}`);
    const name = document.createElement('button');
    name.type = 'button'; name.className = 'sid-name'; name.textContent = g.label;
    name.dataset.id = row.dataset.id; name.dataset.tip = groupTip(g);
    name.setAttribute('aria-expanded', String(open));
    const count = document.createElement('span');
    count.className = 'sid-count'; count.textContent = !open && on ? `${on}/${eps.length}` : String(eps.length);
    row.append(chev, cb, name, count);
    list.appendChild(row);
    if (!open) continue;
    for (const c of eps) {
      const er = document.createElement('div');
      er.className = 'sid-row epoch' + (c.id === S.cur ? ' current' : '');
      er.dataset.id = 'e:' + c.id;
      const ecb = document.createElement('input');
      ecb.type = 'checkbox'; ecb.checked = ticked(c); ecb.dataset.id = er.dataset.id;
      ecb.setAttribute('aria-label', `Compare ${g.label} epoch ${fmtMinute(c.start)} to ${fmtMinute(c.end)}`);
      const sw = document.createElement('span');
      sw.className = 'sid-swatch';
      const slot = plotted.findIndex((p) => p.chan.id === c.id);
      if (plotted.length > 1 && slot >= 0) sw.style.background = `var(${plotColor(slot)})`;
      const en = document.createElement('button');
      en.type = 'button'; en.className = 'sid-name'; en.textContent = `${fmtMinute(c.start)} → ${fmtMinute(c.end)}`;
      en.dataset.id = er.dataset.id; en.dataset.tip = epochTip(c);
      if (c.id === S.cur) en.setAttribute('aria-current', 'true');
      er.append(ecb, sw, en);
      list.appendChild(er);
    }
  }
  if (active) focusRow(active.id, active.box);
}

/** Row ids are 'g:<group key>' or 'e:<epoch id>'. */
function splitId(id) {
  const i = (id || '').indexOf(':');
  return i < 0 ? [null, null] : [id.slice(0, i), id.slice(i + 1)];
}

function focusRow(id, box) {
  const target = [...$('sidList').querySelectorAll(box ? 'input' : 'button')].find((x) => x.dataset.id === id);
  if (target) target.focus();
}

/** Keep the current epoch inside the visible groups, then redraw. */
function renderSidList() {
  const known = new Set(S.channels.map((c) => c.id));
  for (const id of [...S.checked]) if (!known.has(id)) S.checked.delete(id);
  const keys = new Set(sourceGroups().map((g) => g.key));
  for (const k of [...S.expanded]) if (!keys.has(k)) S.expanded.delete(k);
  const groups = visibleGroups();
  const cur = chanOf(S.cur);
  if (!cur || !groups.some((g) => g.key === groupKey(cur))) setCurrent(groups.length ? defaultEpoch(groups[0]) : null);
  selectionChanged();
}

/** The epoch of a group containing now, else its latest one. */
function defaultEpoch(g) {
  const eps = epochsOf(g);
  const now = Date.now();
  return (eps.filter((c) => contains(c, now)).pop() || eps[eps.length - 1]).id;
}

function contains(c, t) {
  return (!c.start || Date.parse(c.start) <= t) && (!c.end || Date.parse(c.end) > t);
}

function selectionChanged() {
  renderRows();
  updateAll();
  recompute();
}

/** Make an epoch the current one (it drives the stage table, summary and text export) and show its group expanded. */
function setCurrent(id) {
  if (id !== S.cur) S.excluded.clear();
  S.cur = id;
  const c = chanOf(id);
  if (c) S.expanded.add(groupKey(c));
  autoFrequencies();
}

/** Ticked epochs stay plotted when another epoch becomes current; ticking also keeps the current epoch. */
function onTick(id, on) {
  if (on) {
    S.checked.add(id);
    if (S.cur !== null) S.checked.add(S.cur);
  } else {
    S.checked.delete(id);
  }
  selectionChanged();
}

function onGroupTick(key, on) {
  const g = groupOf(key);
  if (!g) return;
  const ids = epochsOf(g).map((c) => c.id);
  if (on) {
    for (const id of ids) S.checked.add(id);
    if (S.cur !== null) S.checked.add(S.cur);
  } else {
    for (const id of ids) S.checked.delete(id);
  }
  selectionChanged();
}

function toggleGroup(key, open) {
  if (open === undefined) open = !S.expanded.has(key);
  if (open) S.expanded.add(key); else S.expanded.delete(key);
  renderRows();
}

/** Default the maximum frequency to the channel's Nyquist (unless the user chose a range). */
function channelNyquist(chan) {
  if (!chan) return null;
  const decim = chan.stages.filter((s) => s.sequence > 0 && s.sample_rate && s.decimation_factor);
  if (!decim.length) return null;
  const last = decim[decim.length - 1];
  const nyquist = last.sample_rate / last.decimation_factor / 2;
  return nyquist > 0 ? nyquist : null;
}

function autoFrequencies() {
  if (S.freqTouched) return;
  const nyquist = channelNyquist(chanOf(S.cur));
  if (nyquist) $('fMax').value = String(Number((nyquist * 1.1).toPrecision(3))); // 10% past Nyquist
}

/* ------------------------------------------------------------------ computing */

function buildRequest() {
  return {
    channel: S.cur,
    frequencies: {
      min: Number($('fMin').value), max: Number($('fMax').value), count: Math.round(Number($('fN').value)),
      spacing: document.querySelector('input[name=spacing]:checked').value,
    },
    unit: $('unit').value,
    total_sensitivity: $('totalSens').checked,
    estimated_delay: $('estDelay').checked,
    b62_x: Number($('b62x').value) || 0,
    per_stage: $('perStage').checked,
    exclude_stages: [...S.excluded],
    unwrap: $('unwrap').checked,
  };
}

function validate(req) {
  const f = req.frequencies;
  if (!(f.min >= 0) || !(f.max >= 0)) return 'Frequencies must be zero or positive.';
  if (f.spacing === 'log' && (f.min === 0 || f.max === 0)) return 'Logarithmic spacing needs frequencies above zero.';
  if (!(f.count >= 1 && f.count <= 20000)) return 'Points must be between 1 and 20000.';
  return null;
}

const recompute = debounce(async () => {
  $('alerts').innerHTML = '';
  if (S.cur === null) {
    S.seq++;
    S.last = null; S.request = null; S.results = [];
    drawEmpty();
    updateAll();
    return;
  }
  const req = buildRequest();
  const bad = validate(req);
  if (bad) { showAlert('error', bad); return; }
  const plotted = plottedEpochs();
  const single = plotted.length === 1;
  const mine = ++S.seq;

  // The current epoch uses the stage selection; the others include all stages.
  const out = await Promise.all(plotted.map(async (p, i) => {
    const current = p.chan.id === S.cur;
    const item = { chanId: p.chan.id, label: p.label, color: plotColor(i), current };
    const r = { ...req, channel: item.chanId, per_stage: req.per_stage && single && current, exclude_stages: current ? req.exclude_stages : [] };
    try { item.res = await backend.call('evaluate', JSON.stringify(r)); }
    catch (e) { item.res = { ok: false, error: e.message, offline: e.offline }; }
    return item;
  }));
  if (mine !== S.seq) return; // a newer request superseded this one

  $('alerts').innerHTML = '';
  const multi = out.length > 1;
  let offlineShown = false;
  for (const o of out) {
    const prefix = multi ? o.label + ': ' : '';
    if (o.res.offline) {
      if (!offlineShown) showAlert('error', o.res.error);
      offlineShown = true;
    } else if (!o.res.ok) showAlert('error', prefix + o.res.error);
    else for (const w of o.res.warnings || []) showAlert('warn', prefix + w);
  }
  S.results = out.filter((o) => o.res.ok);
  const cur = S.results.find((o) => o.current);
  S.last = cur ? cur.res : null;
  S.request = S.results.length ? { ...req, per_stage: req.per_stage && single } : null;
  if (req.unit === 'def') {
    const units = new Set(S.results.map((o) => (o.res.info.input_units || '').split(' - ')[0]));
    if (units.size > 1) showAlert('warn', `The plotted responses have different input units (${[...units].join(', ')}); choose an output unit to compare amplitudes.`);
  }
  updateAll();
  if (S.results.length) drawBode(); else drawEmpty();
}, 120);

/* ------------------------------------------------------------------ summary / table */

function updateAll() {
  const plotted = plottedEpochs();
  const several = plotted.length > 1;
  const perStage = $('perStage');
  perStage.disabled = several;
  const perStageLabel = perStage.closest('label');
  if (several) perStageLabel.dataset.tip = 'Stage curves are only drawn when a single response is plotted.';
  else delete perStageLabel.dataset.tip;
  const chan = chanOf(S.cur);
  $('exportBtn').disabled = !S.last;
  const title = $('plotTitle');
  const group = chan ? groupOf(groupKey(chan)) : null;
  title.replaceChildren();
  delete title.dataset.tip;
  const entries = titleEntries();
  if (!entries.length) title.textContent = S.channels.length ? 'No response plotted' : 'No response loaded';
  entries.forEach((p, i) => {
    const line = document.createElement('span');
    line.className = 'title-line';
    if (p.chan.origin) line.dataset.tip = p.chan.origin;
    if (several) {
      const swatch = document.createElement('span');
      swatch.className = 'sid-swatch';
      swatch.style.background = `var(${plotColor(i)})`;
      line.appendChild(swatch);
    }
    line.appendChild(document.createTextNode(p.label));
    for (const text of titleParts(p.chan)) {
      const range = document.createElement('span');
      range.className = 'title-range';
      range.textContent = text;
      line.appendChild(range);
    }
    title.appendChild(line);
  });

  // Name the response the stage table applies to when several are plotted.
  const stageFor = $('stageFor');
  const slot = plotted.findIndex((p) => p.chan.id === S.cur);
  stageFor.hidden = !(group && several && slot >= 0);
  stageFor.innerHTML = stageFor.hidden ? ''
    : `<span class="sid-swatch" style="background:var(${plotColor(slot)})"></span>for <b style="color:var(${plotColor(slot)})">${esc(plotted[slot].label)}</b>`;

  const tbl = $('stageTable'), body = tbl.querySelector('tbody');
  body.innerHTML = '';
  if (!chan) { tbl.hidden = true; $('summary').innerHTML = ''; return; }

  const selected = new Map((S.last ? S.last.info.stages : []).map((s) => [s.sequence, s.selected]));
  for (const st of chan.stages) {
    const tr = document.createElement('tr');
    if (selected.size && selected.get(st.sequence) === false) tr.className = 'off';
    const units = st.input_units ? `${st.input_units.split(' - ')[0]} → ${(st.output_units || '').split(' - ')[0]}` : '';
    const use = document.createElement('td');
    if (st.sequence > 0) {
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.checked = !S.excluded.has(st.sequence);
      cb.setAttribute('aria-label', `Include stage ${st.sequence}`);
      cb.onchange = () => { if (cb.checked) S.excluded.delete(st.sequence); else S.excluded.add(st.sequence); recompute(); };
      use.appendChild(cb);
    }
    tr.appendChild(use);
    const cells = [st.sequence === 0 ? 'Total' : st.sequence, st.kind, fmtNum(st.gain, 6), fmtNum(st.gain_frequency), units,
                   st.sample_rate ? fmtNum(st.sample_rate) : ''];
    cells.forEach((c, i) => {
      const td = document.createElement('td');
      td.textContent = c;
      if (i === 0 && st.sequence > 0 && S.last && S.last.stage_values && String(st.sequence) in S.last.stage_values) {
        td.className = 'stage-num';
        td.style.color = `var(${stageColor(st.sequence)})`;
      }
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }
  tbl.hidden = false;
  const useAll = $('useAll');
  useAll.textContent = S.excluded.size ? 'All' : 'None';
  useAll.title = S.excluded.size ? 'Include every stage in the response' : 'Exclude every stage from the response';

  const sum = $('summary');
  if (S.last) {
    const i = S.last.info;
    sum.innerHTML = `Input <b>${esc((i.input_units || '').split(' - ')[0] || '?')}</b> → output <b>${esc((i.output_units || '').split(' - ')[0] || '?')}</b>; ` +
      `response in <b>${esc(i.requested_unit)}</b>. Sensitivity from stages <b>${fmtNum(i.calculated_sensitivity, 5)}</b> ` +
      `(reported <b>${fmtNum(i.reported_sensitivity, 5)}</b>) at <b>${fmtNum(i.sensitivity_frequency)}</b> Hz.`;
  } else sum.textContent = '';
}

function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

/* ------------------------------------------------------------------ plotting */

const NS = 'http://www.w3.org/2000/svg';
// Plot geometry (SVG user units).  Narrow containers get a narrower, taller plot so text stays legible.
let W = 900, H = 600, AMP_H = 300, PH_H = 0, PLOT_W = 0;
const M = { l: 74, r: 18, t: 14, b: 46 };
const GAP = 22;

function layout() {
  const narrow = $('plotWrap').clientWidth > 0 && $('plotWrap').clientWidth < 600;
  W = narrow ? 520 : 900;
  H = narrow ? 560 : 600;
  AMP_H = narrow ? 290 : 300;
  M.l = narrow ? 62 : 74;
  PH_H = H - M.t - M.b - AMP_H - GAP;
  PLOT_W = W - M.l - M.r;
  return narrow;
}
layout();

function el(name, attrs = {}, parent) {
  const e = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

function drawEmpty() {
  $('plot').toggleAttribute('hidden', true); // SVG elements have no .hidden property
  $('placeholder').hidden = false;
  $('placeholder').textContent = S.cur === null
    ? (S.channels.length ? 'No response matches the filter.' : 'Load a StationXML, RESP or SAC PZ file (or fetch one from an FDSN service), then pick a channel.')
    : 'No response could be computed for these settings.';
}

function niceTicks(lo, hi, target = 6) {
  const span = hi - lo || 1;
  const raw = span / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) ticks.push(Number(v.toPrecision(12)));
  return ticks;
}

function fmtTick(v) {
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1e5 || a < 1e-3)) return v.toExponential(0).replace('e+', 'e');
  return String(Number(v.toPrecision(6)));
}

function finitePairs(xs, ys) { // [[x,y], ...] with breaks (null) where y is not finite/positive
  return xs.map((x, i) => (ys[i] === null || !isFinite(ys[i]) ? null : [x, ys[i]]));
}

function pathFrom(points, fx, fy) {
  let d = '', pen = false;
  for (const p of points) {
    if (!p) { pen = false; continue; }
    d += (pen ? 'L' : 'M') + fx(p[0]).toFixed(2) + ' ' + fy(p[1]).toFixed(2);
    pen = true;
  }
  return d;
}

function drawBode() {
  layout();
  const results = S.results;
  if (!results.length) return drawEmpty();
  const cur = results.find((r) => r.current) || null;
  const svg = $('plot');
  $('plot').toggleAttribute('hidden', false);
  $('placeholder').hidden = true;
  svg.innerHTML = '';
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  el('rect', { x: 0, y: 0, width: W, height: H, class: 'plot-bg' }, svg);

  // ----- datasets (every response has its own frequency list)
  const sets = results.map((r) => ({ name: results.length > 1 ? r.label : 'Total response', color: r.color, f: r.res.frequencies, amp: r.res.amplitude, ph: r.res.phase, main: true }));
  if (results.length === 1 && results[0].res.stage_values) {
    for (const [seq, v] of Object.entries(results[0].res.stage_values)) {
      sets.push({ name: `Stage ${seq}`, color: stageColor(Number(seq)), f: results[0].res.frequencies, amp: v.amplitude, ph: v.phase });
    }
  }
  const mains = sets.filter((s) => s.main);

  const xlog = S.request.frequencies.spacing === 'log' && sets.every((s) => s.f.every((v) => v > 0));
  const tx = xlog ? Math.log10 : (v) => v;
  let xlo = tx(minOf(mains.map((s) => minOf(s.f)))), xhi = tx(maxOf(mains.map((s) => maxOf(s.f))));
  if (xlo === xhi) { xlo -= 0.5; xhi += 0.5; }
  const fx = (v) => M.l + (tx(v) - xlo) / (xhi - xlo) * PLOT_W;

  // ----- amplitude axis (log)
  const positive = (a) => a.filter((v) => v !== null && v > 0 && isFinite(v));
  let ampAll = mains.flatMap((s) => positive(s.amp));
  if (!ampAll.length) ampAll = [1];
  let lo = minOf(ampAll), hi = maxOf(ampAll);
  for (const s of sets.filter((x) => !x.main)) { // include stages, but do not let outliers swamp the total
    const p = positive(s.amp);
    if (p.length) { lo = Math.min(lo, minOf(p)); hi = Math.max(hi, maxOf(p)); }
  }
  const mainLo = minOf(ampAll), mainHi = maxOf(ampAll);
  if (Math.log10(hi / lo) > 18) { lo = mainLo / 1e3; hi = mainHi * 1e3; }
  let ylo = Math.floor(Math.log10(lo)), yhi = Math.ceil(Math.log10(hi));
  if (yhi - ylo < 1) { ylo -= 0; yhi = ylo + 1; }
  const ay = (v) => M.t + (1 - (Math.log10(v) - ylo) / (yhi - ylo)) * AMP_H;

  // ----- phase axis (linear)
  const allPh = sets.flatMap((s) => s.ph.filter((v) => v !== null && isFinite(v)));
  let plo = -180, phi = 180;
  if ($('unwrap').checked && allPh.length) {
    plo = Math.floor(minOf(allPh) / 90) * 90; phi = Math.ceil(maxOf(allPh) / 90) * 90;
    if (phi - plo < 90) phi = plo + 90;
  }
  const phTop = M.t + AMP_H + GAP;
  const py = (v) => phTop + (1 - (v - plo) / (phi - plo)) * PH_H;

  // ----- grid & axes
  const gridV = el('g', {}, svg);
  const xTicks = xlog
    ? (() => { const t = []; for (let e = Math.floor(xlo); e <= Math.ceil(xhi); e++) for (let m = 1; m < 10; m++) { const v = m * Math.pow(10, e); if (tx(v) >= xlo - 1e-9 && tx(v) <= xhi + 1e-9) t.push({ v, major: m === 1 }); } return t; })()
    : niceTicks(xlo, xhi, 8).map((v) => ({ v, major: true }));
  for (const t of xTicks) {
    const x = fx(t.v);
    for (const [y0, h] of [[M.t, AMP_H], [phTop, PH_H]]) el('line', { x1: x, x2: x, y1: y0, y2: y0 + h, class: t.major ? 'grid-line' : 'grid-minor' }, gridV);
    if (t.major) {
      const lab = el('text', { x, y: phTop + PH_H + 19, 'text-anchor': 'middle', class: 'tick-label' }, svg);
      lab.textContent = fmtTick(t.v);
    }
  }
  for (let e = ylo; e <= yhi; e++) {
    const y = ay(Math.pow(10, e));
    el('line', { x1: M.l, x2: M.l + PLOT_W, y1: y, y2: y, class: 'grid-line' }, gridV);
    const lab = el('text', { x: M.l - 8, y: y + 4, 'text-anchor': 'end', class: 'tick-label' }, svg);
    lab.textContent = fmtTick(Math.pow(10, e));
    if (yhi - ylo <= 6 && e < yhi) for (let m = 2; m < 10; m++) {
      const ym = ay(m * Math.pow(10, e));
      el('line', { x1: M.l, x2: M.l + PLOT_W, y1: ym, y2: ym, class: 'grid-minor' }, gridV);
    }
  }
  const phStep = (phi - plo) > 360 ? 90 : 60;
  for (let v = Math.ceil(plo / phStep) * phStep; v <= phi; v += phStep) {
    const y = py(v);
    el('line', { x1: M.l, x2: M.l + PLOT_W, y1: y, y2: y, class: 'grid-line' }, gridV);
    const lab = el('text', { x: M.l - 8, y: y + 4, 'text-anchor': 'end', class: 'tick-label' }, svg);
    lab.textContent = String(v);
  }
  el('rect', { x: M.l, y: M.t, width: PLOT_W, height: AMP_H, class: 'axis-line' }, svg);
  el('rect', { x: M.l, y: phTop, width: PLOT_W, height: PH_H, class: 'axis-line' }, svg);

  const unitLabel = { vel: 'Amplitude (counts per m/s)', dis: 'Amplitude (counts per m)', acc: 'Amplitude (counts per m/s²)', def: 'Amplitude' }[results[0].res.unit] || 'Amplitude';
  const yt = el('text', { transform: `translate(16 ${M.t + AMP_H / 2}) rotate(-90)`, 'text-anchor': 'middle', class: 'axis-title' }, svg);
  yt.textContent = unitLabel;
  const pt = el('text', { transform: `translate(16 ${phTop + PH_H / 2}) rotate(-90)`, 'text-anchor': 'middle', class: 'axis-title' }, svg);
  pt.textContent = 'Phase (degrees)';
  const xt = el('text', { x: M.l + PLOT_W / 2, y: H - 6, 'text-anchor': 'middle', class: 'axis-title' }, svg);
  xt.textContent = 'Frequency (Hz)';

  // ----- curves (clipped to the panels)
  const defs = el('defs', {}, svg);
  for (const [id, y0, h] of [['clipA', M.t, AMP_H], ['clipP', phTop, PH_H]]) {
    const cp = el('clipPath', { id }, defs);
    el('rect', { x: M.l, y: y0, width: PLOT_W, height: h }, cp);
  }
  const gA = el('g', { 'clip-path': 'url(#clipA)' }, svg);
  const gP = el('g', { 'clip-path': 'url(#clipP)' }, svg);
  // reference lines: sensitivity gain / frequency and Nyquist, per response; labels are nudged apart
  if ($('showRefs').checked) {
    const multi = results.length > 1;
    const placed = new Map([[gA, []], [gP, []]]);
    const label = (parent, x, y, text, anchor, dir, color) => {
      const t = el('text', { x, y, 'text-anchor': anchor, class: 'ref-label' }, parent);
      if (multi) t.setAttribute('style', `fill:var(${color})`);
      t.textContent = text;
      const boxes = placed.get(parent);
      const hit = () => { const b = t.getBBox(); return boxes.some((o) => b.x < o.x + o.width && b.x + b.width > o.x && b.y < o.y + o.height && b.y + b.height > o.y); };
      for (let tries = 0, yy = y; hit() && tries < 8; tries++) { yy += dir * 13; t.setAttribute('y', yy); }
      boxes.push(t.getBBox());
    };
    for (const r of results) {
      const info = r.res.info;
      const lineStyle = multi ? `stroke:var(${r.color})` : null;
      const line = (parent, attrs) => { const l = el('line', { ...attrs, class: 'ref-line' }, parent); if (lineStyle) l.setAttribute('style', lineStyle); };
      const sens = info.reported_sensitivity > 0 ? info.reported_sensitivity : info.calculated_sensitivity;
      if (sens > 0 && Math.log10(sens) >= ylo && Math.log10(sens) <= yhi) {
        const y = ay(sens);
        line(gA, { x1: M.l, x2: M.l + PLOT_W, y1: y, y2: y });
        label(gA, M.l + 8, y + 15, `sens gain: ${fmtNum(sens, 6)}`, 'start', 1, r.color);
      }
      const nyquist = channelNyquist(S.channels.find((c) => c.id === r.chanId));
      const vlines = [
        [info.sensitivity_frequency, `sens freq ${fmtNum(info.sensitivity_frequency)} Hz`],
        [nyquist, nyquist ? `nyquist: ${fmtNum(nyquist)} Hz` : null],
      ];
      for (const [v, text] of vlines) {
        if (!(v > 0) || (xlog && v <= 0) || tx(v) < xlo || tx(v) > xhi) continue;
        const x = fx(v);
        for (const [g, y0, h] of [[gA, M.t, AMP_H], [gP, phTop, PH_H]]) {
          line(g, { x1: x, x2: x, y1: y0, y2: y0 + h });
          label(g, x - 4, y0 + h - 8, text, 'end', -1, r.color);
        }
      }
    }
  }

  for (const s of sets.slice().reverse()) {
    const style = `stroke:var(${s.color})`;
    const cls = 'curve' + (s.main ? '' : ' stage');
    const ampPts = finitePairs(s.f, s.amp.map((v) => (v !== null && v > 0 ? v : null)));
    const phPts = finitePairs(s.f, s.ph);
    el('path', { d: pathFrom(ampPts, fx, ay), class: cls, style }, gA);
    el('path', { d: pathFrom(phPts, fx, py), class: cls, style }, gP);
    if ($('showPoints').checked) {
      const r = s.main ? 3.5 : 2.5; // unfilled circles, so the curve stays visible through each symbol
      const dots = (pts, fy) => pts.filter(Boolean).map((p) =>
        `M${(fx(p[0]) - r).toFixed(2)} ${fy(p[1]).toFixed(2)}a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 ${-2 * r} 0`).join('');
      const pcls = 'points' + (s.main ? '' : ' stage');
      el('path', { d: dots(ampPts, ay), class: pcls, style }, gA);
      el('path', { d: dots(phPts, py), class: pcls, style }, gP);
    }
  }

  // ----- hover
  const cursorA = el('line', { class: 'cursor', y1: M.t, y2: M.t + AMP_H, visibility: 'hidden' }, svg);
  const cursorP = el('line', { class: 'cursor', y1: phTop, y2: phTop + PH_H, visibility: 'hidden' }, svg);

  // ----- legend (lower-left corner of the plot, always lists the total response)
  const rowH = 15, pad = 6, swatch = 20;
  const perCol = Math.max(1, Math.min(sets.length, Math.floor((PH_H * 0.6 - 2 * pad) / rowH)));
  const lgBox = el('rect', { class: 'legend-box', rx: 4 }, svg);
  const lgG = el('g', {}, svg);
  const entries = sets.map((s, i) => {
    const line = el('line', { class: 'curve' + (s.main ? '' : ' stage'), style: `stroke:var(${s.color})` }, lgG);
    const t = el('text', { class: 'legend-text' }, lgG);
    t.textContent = s.name;
    return { line, t, col: Math.floor(i / perCol), row: i % perCol };
  });
  const colW = swatch + 5 + maxOf(entries.map((e) => e.t.getComputedTextLength())) + 16;
  for (const e of entries) {
    const x = e.col * colW, y = e.row * rowH;
    e.line.setAttribute('x1', x); e.line.setAttribute('x2', x + swatch);
    e.line.setAttribute('y1', y + rowH / 2); e.line.setAttribute('y2', y + rowH / 2);
    e.t.setAttribute('x', x + swatch + 5); e.t.setAttribute('y', y + rowH / 2 + 4);
  }
  const bb = lgG.getBBox();
  const bw = bb.width + 2 * pad, bh = bb.height + 2 * pad;
  const bx = M.l + 8, by = phTop + PH_H - 8 - bh;
  lgBox.setAttribute('x', bx); lgBox.setAttribute('y', by);
  lgBox.setAttribute('width', bw); lgBox.setAttribute('height', bh);
  lgG.setAttribute('transform', `translate(${bx + pad - bb.x} ${by + pad - bb.y})`);
  const tip = $('tip');
  const onMove = (ev) => {
    const r = svg.getBoundingClientRect();
    const x = (ev.clientX - r.left) * (W / r.width);
    if (x < M.l || x > M.l + PLOT_W) return onLeave();
    const target = xlo + (x - M.l) / PLOT_W * (xhi - xlo);
    const nearest = (fr) => {
      let best = 0, bd = Infinity;
      fr.forEach((v, i) => { const d = Math.abs(tx(v) - target); if (d < bd) { bd = d; best = i; } });
      return best;
    };
    const lead = results[0].res;
    const cx = fx(lead.frequencies[nearest(lead.frequencies)]);
    for (const c of [cursorA, cursorP]) { c.setAttribute('x1', cx); c.setAttribute('x2', cx); c.setAttribute('visibility', 'visible'); }
    tip.hidden = false;
    if (results.length === 1) {
      const i = nearest(lead.frequencies);
      tip.innerHTML = `f ${fmtNum(lead.frequencies[i], 5)} Hz<br>|H| ${fmtNum(lead.amplitude[i], 5)}<br>φ ${fmtNum(lead.phase[i], 5)}°`;
    } else {
      const fHead = lead.frequencies[nearest(lead.frequencies)];
      tip.innerHTML = `f ${fmtNum(fHead, 5)} Hz` + results.map((r) => {
        const i = nearest(r.res.frequencies), fr = r.res.frequencies[i];
        const at = Math.abs(fr - fHead) > 1e-9 * Math.abs(fHead) ? ` @ ${fmtNum(fr, 5)} Hz` : '';
        return `<br><span class="sw" style="background:var(${r.color})"></span>${esc(r.label)}${at}: |H| ${fmtNum(r.res.amplitude[i], 5)}, φ ${fmtNum(r.res.phase[i], 5)}°`;
      }).join('');
    }
    const wr = $('plotWrap').getBoundingClientRect();
    const tw = tip.offsetWidth;
    let left = ev.clientX - wr.left + 14;
    if (left + tw + 4 > wr.width) left = Math.max(0, ev.clientX - wr.left - tw - 14);
    tip.style.left = left + 'px';
    tip.style.top = Math.max(0, ev.clientY - wr.top - 20) + 'px';
  };
  const onLeave = () => { tip.hidden = true; cursorA.setAttribute('visibility', 'hidden'); cursorP.setAttribute('visibility', 'hidden'); };
  svg.onpointermove = onMove;
  svg.onpointerleave = onLeave;
}

/* ------------------------------------------------------------------ export */

/** Title lines for the plotted responses (or the current channel): [{ chan, label }]. */
function titleEntries() {
  const plotted = plottedEpochs();
  if (plotted.length) return plotted.map((p) => ({ chan: p.chan, label: p.group ? p.group.label : p.label }));
  const chan = chanOf(S.cur);
  if (!chan) return [];
  const group = groupOf(groupKey(chan));
  return [{ chan, label: group ? group.label : chan.source_id }];
}

/** Validity range and instrument description shown after a title label. */
function titleParts(chan) {
  const parts = [`${fmtDate(chan.start)} → ${fmtDate(chan.end)}`];
  if (chan.instrument) parts.push(chan.instrument);
  return parts;
}

const TITLE_PAD = 10, TITLE_LINE_H = 18;

/** Title block for the exported plot, drawn into `svg`; returns { g, height } (g is null without a title). */
function exportTitle(svg) {
  const entries = titleEntries();
  if (!entries.length) return { g: null, height: 0 };
  const g = document.createElementNS(NS, 'g');
  svg.appendChild(g); // attached while measuring text
  let y = TITLE_PAD, x0 = TITLE_PAD;
  entries.forEach((p, i) => {
    x0 = TITLE_PAD;
    if (entries.length > 1) {
      el('rect', { x: TITLE_PAD, y: y + TITLE_LINE_H / 2 - 5, width: 14, height: 3, rx: 1.5, style: `fill:var(${plotColor(i)})` }, g);
      x0 += 22;
    }
    const newLine = (indent) => el('text', { x: x0 + indent, y: y + 13, class: 'export-title' }, g);
    let t = newLine(0);
    t.textContent = p.label;
    for (const part of titleParts(p.chan)) {
      const span = el('tspan', { class: 'export-title-sub', dx: 12 }, t);
      span.textContent = part;
      // a part that would overrun the right edge continues on an indented line
      if (t.childNodes.length > 2 && x0 + t.getComputedTextLength() > W - TITLE_PAD) {
        t.removeChild(span);
        y += TITLE_LINE_H;
        t = newLine(12);
        el('tspan', { class: 'export-title-sub' }, t).textContent = part;
      }
    }
    y += TITLE_LINE_H;
  });
  svg.removeChild(g);
  return { g, height: y + TITLE_PAD };
}

async function standaloneSvg() {
  const css = await (await fetch('style.css')).text();
  const rules = css.slice(css.indexOf('/* SVG plot */'));
  const cs = getComputedStyle(document.documentElement);
  const resolve = (s) => s.replace(/var\((--[\w-]+)\)/g, (_, n) => cs.getPropertyValue(n).trim());
  const { g: title, height: hdr } = exportTitle($('plot'));
  const clone = $('plot').cloneNode(true);
  clone.setAttribute('xmlns', NS);
  clone.setAttribute('width', W);
  clone.setAttribute('height', H + hdr);
  clone.setAttribute('viewBox', `0 0 ${W} ${H + hdr}`);
  clone.removeAttribute('hidden');
  if (title) {
    const body = el('g', { transform: `translate(0 ${hdr})` });
    while (clone.firstChild) body.appendChild(clone.firstChild);
    clone.appendChild(el('rect', { x: 0, y: 0, width: W, height: H + hdr, class: 'plot-bg' }));
    clone.appendChild(title);
    clone.appendChild(body);
  }
  const style = document.createElementNS(NS, 'style');
  style.textContent = resolve(rules);
  clone.insertBefore(style, clone.firstChild);
  return { text: resolve(new XMLSerializer().serializeToString(clone)), height: H + hdr };
}

async function doExport(kind) {
  const chan = chanOf(S.cur);
  const stamp = chan && chan.start ? `.${fileStamp(chan.start)}` : '';
  const base = S.results.length > 1 ? 'responses' : chan ? chan.nslc + stamp : 'response';
  if (kind === 'svg') {
    return download(`${base}.svg`, new Blob([(await standaloneSvg()).text], { type: 'image/svg+xml' }));
  }
  if (kind === 'png') {
    const { text: svgText, height } = await standaloneSvg();
    const img = new Image();
    const url = URL.createObjectURL(new Blob([svgText], { type: 'image/svg+xml' }));
    await new Promise((ok, bad) => { img.onload = ok; img.onerror = bad; img.src = url; });
    const canvas = document.createElement('canvas');
    canvas.width = W * 2; canvas.height = height * 2;
    canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
    URL.revokeObjectURL(url);
    return canvas.toBlob((b) => download(`${base}.png`, b), 'image/png');
  }
  const res = await backend.call('export', JSON.stringify(S.request), kind);
  if (!res.ok) { showAlert('error', res.error); return; }
  download(res.filename + stamp, new Blob([res.text], { type: 'text/plain' }));
}

/* ------------------------------------------------------------------ wiring */

function currentTheme() {
  const t = document.documentElement.dataset.theme;
  return t || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
}

/** Tooltips for elements with a data-tip attribute; shown after TIP_DELAY_MS (native titles wait far longer). */
const TIP_DELAY_MS = 80;

function wireTips() {
  const tip = document.createElement('div');
  tip.className = 'hint-tip';
  tip.hidden = true;
  tip.setAttribute('role', 'tooltip');
  document.body.appendChild(tip);
  let timer = 0, current = null, x = 0, y = 0;
  const hide = () => { clearTimeout(timer); tip.hidden = true; };
  const place = () => {
    const r = tip.getBoundingClientRect();
    tip.style.left = Math.max(4, Math.min(x + 12, window.innerWidth - r.width - 4)) + 'px';
    tip.style.top = Math.max(4, Math.min(y + 18, window.innerHeight - r.height - 4)) + 'px';
  };
  document.addEventListener('mouseover', (e) => {
    const target = e.target.closest ? e.target.closest('[data-tip]') : null;
    if (target === current) return;
    current = target;
    hide();
    if (!target) return;
    timer = setTimeout(() => { tip.textContent = target.dataset.tip; tip.hidden = false; place(); }, TIP_DELAY_MS);
  });
  document.addEventListener('mousemove', (e) => { x = e.clientX; y = e.clientY; if (!tip.hidden) place(); });
  document.addEventListener('mouseout', (e) => { if (!e.relatedTarget) { current = null; hide(); } });
  document.addEventListener('mousedown', hide);
  document.addEventListener('keydown', hide);
  window.addEventListener('scroll', hide, true);
}

function wire() {
  wireTips();
  $('useAll').onclick = () => {
    const chan = chanOf(S.cur);
    if (!chan) return;
    if (S.excluded.size) S.excluded.clear();
    else for (const st of chan.stages) if (st.sequence > 0) S.excluded.add(st.sequence);
    updateAll();
    recompute();
  };
  $('themeBtn').onclick = () => {
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('theme', next); } catch (e) { /* storage unavailable */ }
  };
  const help = $('helpDialog');
  $('helpBtn').onclick = () => help.showModal();
  $('helpClose').onclick = () => help.close();
  help.addEventListener('click', (e) => { if (e.target === help) help.close(); });
  document.addEventListener('keydown', (e) => {
    if (e.key !== '?' || help.open || e.ctrlKey || e.metaKey || e.altKey) return;
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
    help.showModal();
  });
  $('openBtn').onclick =() => $('fileInput').click();
  $('fileInput').onchange = (e) => { loadFiles([...e.target.files]); e.target.value = ''; };
  const drop = $('drop');
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => loadFiles([...e.dataTransfer.files]));
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => { e.preventDefault(); if (!drop.contains(e.target)) loadFiles([...e.dataTransfer.files]); });
  $('urlBtn').onclick = loadUrlField;
  $('urlInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadUrlField(); });
  $('fetchBtn').onclick = fetchFdsn;
  $('clearBtn').onclick = clearResponses;

  $('sidFilter').oninput = debounce(() => renderSidList(), 150);
  $('sidList').addEventListener('change', (e) => {
    const [kind, id] = splitId(e.target.dataset.id);
    if (kind === 'g') onGroupTick(id, e.target.checked);
    else if (kind === 'e') onTick(Number(id), e.target.checked);
  });
  $('sidList').addEventListener('click', (e) => {
    const row = e.target.closest('.sid-row');
    if (!row || e.target.tagName === 'INPUT') return;
    const [kind, id] = splitId(row.dataset.id);
    if (kind === 'g') toggleGroup(id);
    else if (kind === 'e' && Number(id) !== S.cur) { setCurrent(Number(id)); selectionChanged(); }
  });
  $('sidList').addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT') return;
    const [kind, id] = splitId(e.target.dataset.id);
    if (kind === 'g' && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
      e.preventDefault();
      toggleGroup(id, e.key === 'ArrowRight');
      return;
    }
    const step = e.key === 'ArrowDown' ? 1 : e.key === 'ArrowUp' ? -1 : 0;
    if (!step) return;
    const names = [...$('sidList').querySelectorAll('button.sid-name')];
    const next = names[names.indexOf(e.target) + step];
    if (!next) return;
    e.preventDefault();
    next.focus();
    const [nextKind, nextId] = splitId(next.dataset.id);
    if (nextKind === 'e' && Number(nextId) !== S.cur) { setCurrent(Number(nextId)); selectionChanged(); }
  });

  for (const id of ['fMin', 'fMax']) $(id).addEventListener('input', () => { S.freqTouched = true; });
  for (const id of ['fMin', 'fMax', 'fN', 'unit', 'perStage', 'unwrap', 'totalSens', 'estDelay', 'b62x']) {
    $(id).addEventListener('input', recompute);
    $(id).addEventListener('change', recompute);
  }
  for (const id of ['showRefs', 'showPoints']) $(id).addEventListener('change', () => { if (S.last) drawBode(); });
  document.querySelectorAll('input[name=spacing]').forEach((r) => r.addEventListener('change', recompute));

  const menu = $('exportMenu'), btn = $('exportBtn');
  btn.onclick = (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; btn.setAttribute('aria-expanded', String(!menu.hidden)); };
  document.addEventListener('click', () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); });
  menu.addEventListener('click', (e) => { const k = e.target.dataset.export; if (k) doExport(k); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') menu.hidden = true; });

  let wasNarrow = layout();
  window.addEventListener('resize', debounce(() => {
    const narrow = layout();
    if (narrow !== wasNarrow) { wasNarrow = narrow; if (S.last) drawBode(); }
  }, 150));
}

async function main() {
  wire();
  try {
    await backend.init();
    setBadge(backend.kind === 'http' ? 'local Python' : 'Python in browser (Pyodide)', true);
    try { $('version').textContent = 'v' + (await backend.call('version')).version; } catch (e) { /* optional */ }
    await refreshChannels();
    const urls = new URLSearchParams(location.search).getAll('url').map((u) => u.trim()).filter(Boolean);
    if (urls.length) { $('urlInput').value = urls[0]; await loadUrls(urls); }
  } catch (e) {
    setBadge('backend failed');
    showAlert('error', 'Could not start the computation backend: ' + e.message);
  }
}

window.__seismic = { S, backend, loadFiles, loadUrls, refreshChannels, standaloneSvg }; // handy for debugging and tests
main();

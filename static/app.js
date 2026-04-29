// sysdash UI - WS client + render loop + actions.

const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...children) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'html') e.innerHTML = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return e;
};

let lastNotifiedAlerts = new Set();
const trendHistory = { cpu: [], ram: [], disk: [], net: [] };
const THEME_STORE = 'sysdash_theme_v1';
const LEARNING_STORE = 'sysdash_learning_mode_v1';
const COLLAPSE_STORE = 'sysdash_collapsed_cards_v1';
const CARD_ORDER_STORE = 'sysdash_card_order_v1';
const PINNED_CARDS_STORE = 'sysdash_pinned_cards_v1';
let collapsedCards = new Set(JSON.parse(localStorage.getItem(COLLAPSE_STORE) || '[]'));
let pinnedCards = new Set(JSON.parse(localStorage.getItem(PINNED_CARDS_STORE) || '[]'));
let lastPackages = [];
let packageFilters = { search: '', manager: 'all', status: 'all' };
let activeConfig = null;
let lastCheats = [];
let llmChatServers = [];
let llmChatHistory = [];

// Ask for notification permission once.
if ('Notification' in window && Notification.permission === 'default') {
  // Defer until user interaction
  document.addEventListener('click', () => Notification.requestPermission(), { once: true });
}

function notify(title, body) {
  if ('Notification' in window && Notification.permission === 'granted') {
    new Notification(title, { body });
  }
}

function pctClass(pct, warn = 70, bad = 90) {
  if (pct >= bad) return 'bad';
  if (pct >= warn) return 'warn';
  return '';
}

function bar(pct, klass = '') {
  pct = Math.max(0, Math.min(100, pct));
  return `<div class="bar ${klass}"><span style="width:${pct}%"></span></div>`;
}

function row(label, val, klass = '') {
  return `<div class="metric-row ${klass}"><span class="label">${label}</span><span class="val">${val}</span></div>`;
}

function pushTrend(key, value, max = 34) {
  if (!trendHistory[key]) trendHistory[key] = [];
  const arr = trendHistory[key];
  arr.push(Number(value) || 0);
  while (arr.length > max) arr.shift();
  return arr;
}

function spark(values) {
  const arr = values.length > 1 ? values : [0, values[0] || 0];
  const max = Math.max(...arr, 1);
  const min = Math.min(...arr, 0);
  const span = Math.max(max - min, 1);
  const points = arr.map((v, i) => {
    const x = (i / Math.max(arr.length - 1, 1)) * 96;
    const y = 22 - ((v - min) / span) * 20;
    return [x, y];
  });
  const line = points.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const area = `${line} L96,24 L0,24 Z`;
  return `<svg class="sparkline" viewBox="0 0 96 24" aria-hidden="true"><path class="area" d="${area}"></path><path class="line" d="${line}"></path></svg>`;
}

function trend(label, values) {
  return `<div class="trend-row"><span>${label}</span>${spark(values)}</div>`;
}

function setCc(id, value, klass = '') {
  const node = $(id);
  if (!node) return;
  node.textContent = value;
  node.className = `cc-value ${klass}`.trim();
}

function setCcMeter(id, pct) {
  const node = $(id);
  if (!node) return;
  node.style.width = `${Math.max(0, Math.min(100, Number(pct) || 0))}%`;
}

function linesFromTextarea(id) {
  return ($(id)?.value || '').split('\n').map(x => x.trim()).filter(Boolean);
}

function setInput(id, value) {
  const node = $(id);
  if (node) node.value = value ?? '';
}

function setChecked(id, value) {
  const node = $(id);
  if (node) node.checked = !!value;
}

// ---- modal confirm ----
function confirmAction(title, body, onConfirm) {
  $('modal')?.classList.remove('process-modal');
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = body;
  $('modal-confirm').style.display = '';
  $('modal-bg').classList.add('show');
  const close = () => $('modal-bg').classList.remove('show');
  const c = $('modal-confirm');
  const x = $('modal-cancel');
  const onYes = () => { close(); cleanup(); onConfirm(); };
  const onNo = () => { close(); cleanup(); };
  function cleanup() {
    c.removeEventListener('click', onYes);
    x.removeEventListener('click', onNo);
  }
  c.addEventListener('click', onYes);
  x.addEventListener('click', onNo);
}

async function postAction(url) {
  const r = await fetch(url, { method: 'POST' });
  return r.json();
}

async function postJson(url, payload) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return r.json();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;',
  }[ch]));
}

// ---- renderers ----
function render(m) {
  $('ts').textContent = new Date(m.ts * 1000).toLocaleTimeString();
  renderAlerts(m.alerts || [], m.disk);
  renderCpu(m.system?.cpu);
  renderRam(m.system?.ram);
  renderTopRam(m.system?.top_ram || []);
  renderTopCpu(m.system?.top_cpu || []);
  renderDisk(m.disk);
  renderNet(m.net);
  renderSysdashFootprint(m.self);
  renderPorts(m.ports || [], m.port_health || []);
  renderActivePorts(m.active_ports || []);
  renderStatusPanel(m);
  renderRuntimes(m.runtimes?.processes || []);
  renderLlms(m.llms || {});
  renderGit(m.git || []);
  renderToolchain(m.toolchain || []);
  renderPackages(m.packages || []);
  renderLogs(m.log_errors || []);
  renderBattery(m.battery_thermals || {});
  renderCheats(m.cheatsheet || []);
  renderDiagnostic(m.diagnostic || []);
  renderDiskHogs(m.disk_hogs || []);
  renderDataHogs(m.data_hogs || []);
  renderCleanupCenter();
  renderTopClock(m);
  renderCommandCenter(m);
  renderTopHealth(m);
}

function renderAlerts(alerts, disk) {
  const a = $('alerts');
  if (!alerts.length) { a.classList.remove('has'); a.innerHTML = ''; return; }
  a.classList.add('has');
  const diskAlerts = alerts.filter(x => x.key?.startsWith('disk:'));
  const otherAlerts = alerts.filter(x => !x.key?.startsWith('disk:'));
  const diskHtml = diskAlerts.map(alert => {
    const mount = alert.key.split(':').slice(1).join(':');
    const part = (disk?.partitions || []).find(p => p.mount === mount);
    if (!part) return `<div class="alert-pill ${alert.level}"><span class="dot ${alert.level === 'red' ? 'bad' : 'warn'}"></span>${escapeHtml(alert.msg)}</div>`;
    const freePct = Math.max(0, 100 - Number(part.pct || 0));
    const usedPct = Math.max(0, Math.min(100, Number(part.pct || 0)));
    return `
      <div class="disk-alert-card">
        <div class="disk-alert-copy">
          <span class="disk-alert-title">💾 Storage pressure</span>
          <strong>${escapeHtml(part.mount)}</strong>
          <span>${Number(part.free_gb || 0).toFixed(1)} GB free · ${freePct.toFixed(0)}% free</span>
        </div>
        <div class="disk-alert-meter" aria-label="${usedPct.toFixed(0)} percent used"><span style="width:${usedPct}%"></span></div>
        <div class="disk-alert-actions">
          <button type="button" data-alert-jump="disk-hogs">Disk hogs</button>
          <button type="button" data-alert-jump="cleanup-center">Cleanup</button>
          <button type="button" data-alert-cleanup>Recommended</button>
        </div>
      </div>
    `;
  }).join('');
  const otherHtml = otherAlerts.map(x => `<div class="alert-pill ${x.level}"><span class="dot ${x.level === 'red' ? 'bad' : 'warn'}"></span>${escapeHtml(x.msg)}</div>`).join('');
  a.innerHTML = `${diskHtml}${otherHtml ? `<div class="alert-pills">${otherHtml}</div>` : ''}`;
  a.querySelectorAll('[data-alert-jump]').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = $(btn.dataset.alertJump);
      const card = target?.closest('.card') || target;
      if (!card) return;
      card.classList.remove('is-collapsed');
      card.classList.add('focus-flash');
      setTimeout(() => card.classList.remove('focus-flash'), 900);
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  });
  a.querySelectorAll('[data-alert-cleanup]').forEach(btn => {
    btn.addEventListener('click', () => $('cleanup-recommended')?.click());
  });
  // Notify only on new alerts.
  for (const al of alerts) {
    if (!lastNotifiedAlerts.has(al.key)) {
      notify('sysdash alert', al.msg);
      lastNotifiedAlerts.add(al.key);
    }
  }
  // Forget cleared alerts so they re-fire.
  const live = new Set(alerts.map(a => a.key));
  for (const k of Array.from(lastNotifiedAlerts)) if (!live.has(k)) lastNotifiedAlerts.delete(k);
}

function renderTopClock(m) {
  const timeNode = $('clock-time');
  const dateNode = $('clock-date');
  const detailNode = $('clock-details');
  if (!timeNode || !dateNode || !detailNode) return;
  const when = new Date(m.ts * 1000);
  const update = when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const date = when.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
  const host = m.host || {};
  const details = [
    host.hostname,
    host.machine,
    host.ip ? `IP ${host.ip}` : '',
  ].filter(Boolean).join(' · ');
  timeNode.textContent = update;
  dateNode.textContent = date;
  detailNode.textContent = details;
}

function renderCommandCenter(m) {
  const cpu = m.system?.cpu?.overall_pct ?? 0;
  const ram = m.system?.ram?.pct ?? 0;
  const swap = m.system?.ram?.swap_pct ?? 0;
  const net = m.net || {};
  const ports = m.ports || [];
  const outdated = (m.packages || []).filter(p => p.status === 'outdated').length;
  const alerts = m.alerts || [];
  setCc('cc-cpu', `${cpu.toFixed(0)}%`, pctClass(cpu));
  setCc('cc-ram', `${ram.toFixed(0)}%`, pctClass(ram, 70, 85));
  setCc('cc-swap', `${swap.toFixed(0)}%`, swap > 50 ? 'bad' : swap > 20 ? 'warn' : 'ok');
  setCc('cc-net', `${net.down_kbps ?? 0}/${net.up_kbps ?? 0} KB/s`);
  setCc('cc-ports', `${ports.length}`, ports.length > 15 ? 'warn' : 'ok');
  setCc('cc-updates', outdated ? `${outdated}` : '0', outdated ? 'warn' : 'ok');
  setCc('cc-alerts', alerts.length ? `${alerts.length}` : '0', alerts.some(a => a.level === 'red') ? 'bad' : alerts.length ? 'warn' : 'ok');
  setCcMeter('cc-cpu-meter', cpu);
  setCcMeter('cc-ram-meter', ram);
  setCcMeter('cc-swap-meter', swap);
  setCcMeter('cc-net-meter', Math.min(100, ((Number(net.down_kbps || 0) + Number(net.up_kbps || 0)) / 500) * 100));
  setCcMeter('cc-ports-meter', Math.min(100, (ports.length / 25) * 100));
  setCcMeter('cc-updates-meter', Math.min(100, (outdated / 25) * 100));
  setCcMeter('cc-alerts-meter', Math.min(100, (alerts.length / 5) * 100));
}

function renderTopHealth(m) {
  const dot = $('top-health-dot');
  const text = $('top-health-text');
  if (!dot || !text) return;
  const alerts = m.alerts || [];
  const red = alerts.some(a => a.level === 'red');
  const warn = alerts.length > 0;
  dot.className = red ? 'bad' : warn ? 'warn' : '';
  if (red) text.textContent = `🚨 ${alerts.length} urgent issue${alerts.length === 1 ? '' : 's'}`;
  else if (warn) text.textContent = `⚠️ ${alerts.length} warning${alerts.length === 1 ? '' : 's'}`;
  else text.textContent = '✅ all systems steady';
}

function renderCpu(c) {
  if (!c) return;
  const klass = pctClass(c.overall_pct);
  $('cpu-block').innerHTML =
    row('overall', `${c.overall_pct.toFixed(1)}%`, klass) +
    bar(c.overall_pct, klass) +
    row('load avg', `${c.load_avg.map(x => x.toFixed(2)).join(' / ')}`) +
    row('cores', `${c.core_count}`) +
    trend('CPU trend', pushTrend('cpu', c.overall_pct));
  $('cpu-cores').innerHTML = (c.per_core || []).map(p => {
    const k = pctClass(p);
    return `<div class="core ${k}"><i style="height:${p}%"></i><span>${Math.round(p)}</span></div>`;
  }).join('');
  $('cpu-explain').textContent = c.load_explainer || '';
}

function renderRam(r) {
  if (!r) return;
  const klass = pctClass(r.pct, 70, 85);
  $('ram-block').innerHTML =
    row('used', `${r.used_gb} / ${r.total_gb} GB`, klass) +
    bar(r.pct, klass) +
    row('available', `${r.available_gb} GB`) +
    row('swap', `${r.swap_used_gb} GB (${r.swap_pct}%)`, r.swap_pct > 50 ? 'bad' : '') +
    row('pressure', `<span class="dot ${r.pressure?.level === 'green' ? 'ok' : r.pressure?.level === 'yellow' ? 'warn' : r.pressure?.level === 'red' ? 'bad' : 'dim'}"></span>${r.pressure?.level || '?'} (${r.pressure?.free_pct ?? '-'}% free)`) +
    trend('RAM trend', pushTrend('ram', r.pct));
  $('ram-explain').textContent = r.pressure?.explainer || '';
}

function procTable(rows, kind) {
  const t = el('thead', {}, el('tr', {}, ...['pid', 'name', 'user', kind === 'cpu' ? 'cpu%' : 'rss MB', ''].map(h => el('th', {}, h))));
  const body = el('tbody');
  rows.forEach(p => {
    const valCell = kind === 'cpu' ? `${p.cpu_pct.toFixed(1)}` : `${p.rss_mb}`;
    const tr = el('tr', {});
    tr.innerHTML = `<td>${p.pid}</td><td title="Inspect ${escapeHtml(p.name)}"><button class="process-name inspect-process">${escapeHtml(p.name)}</button></td><td>${p.user || ''}</td><td>${valCell}</td><td class="process-actions"><button class="btn-go inspect-process">🔎 inspect</button><button class="btn-x kill-process">✕ kill</button></td>`;
    tr.querySelectorAll('.inspect-process').forEach(btn => btn.addEventListener('click', () => showProcessDetail(p.pid)));
    tr.querySelector('.kill-process').addEventListener('click', () => {
      confirmAction('Kill process?', `Send SIGTERM to <b>${p.name}</b> (PID ${p.pid})?`, async () => {
        const r = await postAction(`/api/kill/${p.pid}`);
        if (!r.ok) alert(`Could not kill: ${r.detail || JSON.stringify(r)}`);
      });
    });
    body.appendChild(tr);
  });
  return [t, body];
}

async function showProcessDetail(pid) {
  const body = $('modal-body');
  $('modal')?.classList.add('process-modal');
  $('modal-title').textContent = 'Process helper';
  body.innerHTML = '<div class="no-data">Inspecting process...</div>';
  $('modal-confirm').style.display = 'none';
  $('modal-bg').classList.add('show');
  try {
    const r = await fetch(`/api/process/${pid}`);
    const p = await r.json();
    if (!p.ok) {
      body.innerHTML = `<div class="no-data">${escapeHtml(p.detail || 'Could not inspect process.')}</div>`;
      return;
    }
    const detailRow = (label, value, extra = '') => (
      `<div class="process-detail-row ${extra}"><span>${escapeHtml(label)}</span><strong>${value}</strong></div>`
    );
    const ports = (p.ports || []).map(x => (
      `<div class="preview-row"><span>${escapeHtml(x.local || '-')}</span><span class="kv">${escapeHtml([x.status, x.remote].filter(Boolean).join(' -> ') || '-')}</span></div>`
    )).join('');
    body.innerHTML = `
      <div class="process-inspector">
        ${detailRow('name', escapeHtml(p.name || '-'))}
        ${detailRow('pid', p.pid)}
        ${detailRow('status', escapeHtml(p.status || '-'))}
        ${detailRow('cpu', `${Number(p.cpu_pct || 0).toFixed(1)}%`)}
        ${detailRow('memory', `${Number(p.rss_mb || 0).toFixed(1)} MB`)}
        ${detailRow('threads', p.threads || 0)}
        ${detailRow('parent', p.parent ? `${escapeHtml(p.parent.name)} (${p.parent.pid})` : '-')}
        ${detailRow('cwd', escapeHtml(p.cwd || '-'), 'path-row')}
        ${detailRow('app path', escapeHtml(p.exe || '-'), 'path-row')}
        ${detailRow('command', escapeHtml(p.cmd || '-'), 'command-row')}
        <div class="process-guess">${escapeHtml(p.guess || '')}</div>
        ${ports ? `<div class="preview-list">${ports}</div>` : ''}
      </div>
    `;
  } catch (e) {
    body.innerHTML = `<div class="no-data">Could not inspect process: ${escapeHtml(e)}</div>`;
  }
}

function renderTopRam(rows) {
  const tbl = $('top-ram'); tbl.innerHTML = '';
  if (!rows.length) { tbl.innerHTML = '<tr><td class="no-data">no data</td></tr>'; return; }
  procTable(rows, 'ram').forEach(x => tbl.appendChild(x));
}
function renderTopCpu(rows) {
  const tbl = $('top-cpu'); tbl.innerHTML = '';
  if (!rows.length) { tbl.innerHTML = '<tr><td class="no-data">no data</td></tr>'; return; }
  procTable(rows, 'cpu').forEach(x => tbl.appendChild(x));
}

function renderDisk(d) {
  if (!d) return;
  let html = '';
  for (const p of d.partitions || []) {
    const klass = pctClass(p.pct, 75, 90);
    html += row(p.mount, `${p.used_gb} / ${p.total_gb} GB (${p.free_gb} free)`, klass) + bar(p.pct, klass);
  }
  html += row('total read', `${d.io?.read_mb ?? 0} MB`);
  html += row('total write', `${d.io?.write_mb ?? 0} MB`);
  html += trend('I/O trend', pushTrend('disk', (d.io?.read_mb ?? 0) + (d.io?.write_mb ?? 0)));
  $('disk-block').innerHTML = html;
}

function renderNet(n) {
  if (!n) return;
  $('net-block').innerHTML =
    row('connections', n.active_connections) +
    row('traffic', `↓ ${n.down_kbps} KB/s · ↑ ${n.up_kbps} KB/s`) +
    trend('Network trend', pushTrend('net', (n.down_kbps || 0) + (n.up_kbps || 0)));
}

function formatUptime(sec) {
  sec = Number(sec || 0);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

function renderSysdashFootprint(s) {
  const div = $('sysdash-footprint');
  if (!div) return;
  if (!s) { div.innerHTML = '<div class="no-data">checking...</div>'; return; }
  const cpuClass = pctClass(Number(s.cpu_pct || 0), 8, 20);
  const memClass = Number(s.rss_mb || 0) > 900 ? 'bad' : Number(s.rss_mb || 0) > 500 ? 'warn' : '';
  div.innerHTML =
    row('cpu', `${Number(s.cpu_pct || 0).toFixed(1)}%`, cpuClass) +
    row('memory', `${Number(s.rss_mb || 0).toFixed(1)} MB`, memClass);
}

function renderPorts(ports, healths) {
  const tbl = $('ports'); tbl.innerHTML = '';
  if (!ports.length) { tbl.innerHTML = '<tr><td class="no-data">no listeners</td></tr>'; return; }
  const healthMap = {};
  for (const h of healths) healthMap[h.port] = h;
  const head = el('thead', {}, el('tr', {}, ...['port', 'process', 'pid', 'health', ''].map(h => el('th', {}, h))));
  const body = el('tbody');
  ports.forEach(p => {
    const h = healthMap[p.port];
    let healthCell = '<span class="dot dim"></span>—';
    if (h) {
      if (h.ok) healthCell = `<span class="dot ok"></span>${h.status}`;
      else if (h.status) healthCell = `<span class="dot warn"></span>${h.status}`;
      else healthCell = `<span class="dot bad"></span>down`;
    }
    const tr = el('tr', {});
    tr.innerHTML = `<td>${p.port}</td><td>${p.process}</td><td>${p.pid ?? ''}</td><td>${healthCell}</td><td><button class="btn-x">🔓 free</button></td>`;
    tr.querySelector('button').addEventListener('click', () => {
      confirmAction('Free port?', `Kill whatever is listening on port <b>${p.port}</b>?`, async () => {
        const r = await postAction(`/api/free-port/${p.port}`);
        if (!r.ok) alert(`Nothing killed on :${p.port}`);
      });
    });
    body.appendChild(tr);
  });
  tbl.appendChild(head); tbl.appendChild(body);
}

function renderActivePorts(rows) {
  const tbl = $('active-ports'); tbl.innerHTML = '';
  if (!rows.length) { tbl.innerHTML = '<tr><td class="no-data">no active TCP connections</td></tr>'; return; }
  const head = el('thead', {}, el('tr', {}, ...['process', 'pid', 'local', 'remote', ''].map(h => el('th', {}, h))));
  const body = el('tbody');
  rows.forEach(p => {
    const tr = el('tr', {});
    tr.innerHTML = `<td>${p.process}</td><td>${p.pid ?? ''}</td><td class="mono-clip" title="${p.local}">${p.local}</td><td class="mono-clip" title="${p.remote}">${p.remote}</td><td>${p.pid ? '<button class="btn-x">✕ close</button>' : ''}</td>`;
    const btn = tr.querySelector('button');
    if (btn) {
      btn.addEventListener('click', () => {
        confirmAction('Close connection?', `Close <b>${p.local}</b> → <b>${p.remote}</b>?<br><br>This terminates <b>${p.process}</b> (PID ${p.pid}), which may close more than one connection.`, async () => {
          const x = await postAction(`/api/kill/${p.pid}`);
          if (!x.ok) alert(`Could not close: ${x.detail || JSON.stringify(x)}`);
        });
      });
    }
    body.appendChild(tr);
  });
  tbl.appendChild(head); tbl.appendChild(body);
}

function renderDocker(rows) {
  const tbl = $('docker'); tbl.innerHTML = '';
  if (!rows.length) { tbl.innerHTML = '<tr><td class="no-data">none</td></tr>'; return; }
  const head = el('thead', {}, el('tr', {}, ...['name', 'image', 'status', ''].map(h => el('th', {}, h))));
  const body = el('tbody');
  rows.forEach(c => {
    const tr = el('tr', {});
    const action = c.running ? 'stop' : 'start';
    const actionLabel = c.running ? '⏹ stop' : '▶️ start';
    tr.innerHTML = `<td>${c.name}</td><td>${c.image}</td><td>${c.status}</td><td><button class="btn-go">${actionLabel}</button></td>`;
    tr.querySelector('button').addEventListener('click', () => {
      confirmAction(`${action} container?`, `${action} <b>${c.name}</b>?`, async () => {
        await postAction(`/api/docker/${action}/${c.id}`);
      });
    });
    body.appendChild(tr);
  });
  tbl.appendChild(head); tbl.appendChild(body);
}

function renderServices(rows) {
  const tbl = $('services'); tbl.innerHTML = '';
  if (!rows.length) { tbl.innerHTML = '<tr><td class="no-data">brew not available or no services</td></tr>'; return; }
  const head = el('thead', {}, el('tr', {}, ...['name', 'status'].map(h => el('th', {}, h))));
  const body = el('tbody');
  rows.forEach(s => {
    const tr = el('tr', {});
    const klass = s.status === 'started' ? 'ok' : s.status === 'error' ? 'bad' : 'dim';
    tr.innerHTML = `<td>${s.name}</td><td><span class="dot ${klass}"></span>${s.status}</td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(head); tbl.appendChild(body);
}

function renderStatusPanel(m) {
  const div = $('status-panel');
  if (!div) return;
  const inet = m.internet || {};
  const auth = m.auth || [];
  const services = m.services || [];
  const sshvpn = m.ssh_vpn || {};
  const docker = m.docker || [];
  const online = Object.values(inet).filter(Boolean).length;
  const inetTotal = Object.keys(inet).length;
  const authOk = auth.filter(x => x.installed && x.ok).length;
  const authInstalled = auth.filter(x => x.installed).length;
  const serviceStarted = services.filter(x => x.status === 'started').length;
  const dockerRunning = docker.filter(x => x.running).length;
  const sshCount = sshvpn.ssh?.length || 0;
  const vpnCount = sshvpn.vpn?.length || 0;
  const authRows = auth.slice(0, 5).map(a => `
    <span class="status-pill"><span class="dot ${!a.installed ? 'dim' : a.ok ? 'ok' : 'bad'}"></span>${escapeHtml(a.name)}</span>
  `).join('');
  div.innerHTML = `
    <div class="status-grid">
      <div><strong>${online}/${inetTotal || 0}</strong><span>internet checks</span></div>
      <div><strong>${authOk}/${authInstalled || 0}</strong><span>auth logins</span></div>
      <div><strong>${serviceStarted}</strong><span>brew services</span></div>
      <div><strong>${dockerRunning}/${docker.length}</strong><span>docker running</span></div>
      <div><strong>${sshCount}</strong><span>ssh sessions</span></div>
      <div><strong>${vpnCount}</strong><span>vpn interfaces</span></div>
    </div>
    <div class="status-pills">${authRows || '<span class="no-data">auth checking...</span>'}</div>
  `;
}

function renderRuntimes(rows) {
  const tbl = $('runtimes'); tbl.innerHTML = '';
  if (!rows.length) { tbl.innerHTML = '<tr><td class="no-data">no node/python/etc procs</td></tr>'; return; }
  const head = el('thead', {}, el('tr', {}, ...['pid', 'rt', 'rss', 'cmd', ''].map(h => el('th', {}, h))));
  const body = el('tbody');
  rows.forEach(r => {
    const tr = el('tr', {});
    tr.innerHTML = `<td>${r.pid}</td><td>${r.runtime}</td><td>${r.rss_mb}</td><td class="mono-clip" title="${r.cmd}">${r.cmd}</td><td><button class="btn-x">✕ kill</button></td>`;
    tr.querySelector('button').addEventListener('click', () => {
      confirmAction('Kill process?', `Send SIGTERM to ${r.runtime} (PID ${r.pid})?`, async () => {
        const x = await postAction(`/api/kill/${r.pid}`);
        if (!x.ok) alert(`Could not kill: ${x.detail || JSON.stringify(x)}`);
      });
    });
    body.appendChild(tr);
  });
  tbl.appendChild(head); tbl.appendChild(body);
}

function renderLlms(data) {
  const div = $('llms');
  if (!div) return;
  const servers = data.servers || [];
  const processes = data.processes || [];
  const summary = data.summary || {};
  updateLlmChatOptions(servers);
  const serverHtml = servers.map(s => {
    const cls = s.status === 'api' ? 'ok' : s.status === 'listening' ? 'warn' : 'dim';
    const modelText = s.model_count ? `${s.model_count} model${s.model_count === 1 ? '' : 's'}` : s.status === 'offline' ? 'offline' : 'no model list';
    const tps = s.tokens_per_sec == null ? 'n/a' : `${Number(s.tokens_per_sec).toFixed(1)} tok/s`;
    const tpsLabel = s.tokens_per_sec == null ? (s.tps_source || 'unavailable') : s.tps_source === 'avg sample' ? `avg${s.tps_samples ? ` · ${s.tps_samples}` : ''}` : 'live';
    const names = (s.models || []).length ? `<div class="llm-models">${s.models.map(x => `<span>${escapeHtml(x)}</span>`).join('')}</div>` : '';
    return `
      <div class="llm-server ${cls}">
        <div>
          <strong><span class="dot ${cls}"></span>${escapeHtml(s.name)}</strong>
          <span>${escapeHtml(modelText)} · :${s.port}${s.pid ? ` · PID ${s.pid}` : ''}</span>
        </div>
        <div class="llm-tps ${s.tokens_per_sec == null ? 'dim' : 'ok'}"><b>${escapeHtml(tps)}</b><span>${escapeHtml(tpsLabel)}</span></div>
        ${names}
      </div>
    `;
  }).join('');
  const procHtml = processes.length ? `
    <table class="llm-processes">
      <thead><tr><th>pid</th><th>process</th><th>ram</th><th>cpu</th></tr></thead>
      <tbody>${processes.map(p => `<tr><td>${p.pid}</td><td class="mono-clip" title="${escapeHtml(p.cmd || p.name)}">${escapeHtml(p.name)}</td><td>${p.rss_mb}</td><td>${p.cpu_pct}%</td></tr>`).join('')}</tbody>
    </table>
  ` : '<div class="no-data">No local LLM processes detected.</div>';
  div.innerHTML = `
    <div class="llm-summary">
      <div><strong>${summary.active_servers || 0}</strong><span>servers</span></div>
      <div><strong>${summary.model_count || 0}</strong><span>models</span></div>
      <div><strong>${Number(summary.tokens_per_sec || 0).toFixed(1)}</strong><span>avg tok/sec</span></div>
      <div><strong>${Number(summary.ram_mb || 0).toFixed(0)}</strong><span>MB RAM</span></div>
    </div>
    <div class="llm-server-list">${serverHtml}</div>
    ${procHtml}
  `;
}

function updateLlmChatOptions(servers) {
  llmChatServers = servers.filter(s => s.status === 'api');
  const serverSelect = $('llm-chat-server');
  const modelSelect = $('llm-chat-model');
  const status = $('llm-chat-status');
  if (!serverSelect || !modelSelect) return;

  const currentPort = serverSelect.value;
  serverSelect.innerHTML = '';
  if (!llmChatServers.length) {
    serverSelect.appendChild(el('option', { value: '' }, 'no local LLM online'));
    modelSelect.innerHTML = '<option value="">no models</option>';
    if (status) status.textContent = 'offline until a local LLM API is detected';
    return;
  }

  llmChatServers.forEach(s => {
    serverSelect.appendChild(el('option', { value: String(s.port) }, `${s.name} :${s.port}`));
  });
  if (currentPort && llmChatServers.some(s => String(s.port) === currentPort)) {
    serverSelect.value = currentPort;
  }
  refreshLlmChatModels();
  if (status) status.textContent = 'ready';
}

function refreshLlmChatModels() {
  const serverSelect = $('llm-chat-server');
  const modelSelect = $('llm-chat-model');
  if (!serverSelect || !modelSelect) return;
  const server = llmChatServers.find(s => String(s.port) === serverSelect.value) || llmChatServers[0];
  modelSelect.innerHTML = '';
  const models = server?.models || [];
  if (!models.length) {
    modelSelect.appendChild(el('option', { value: '' }, 'default model'));
    return;
  }
  models.forEach(model => modelSelect.appendChild(el('option', { value: model }, model)));
}

function renderLlmChatLog() {
  const log = $('llm-chat-log');
  if (!log) return;
  if (!llmChatHistory.length) {
    log.innerHTML = '<div class="llm-chat-empty">Start LM Studio or Ollama, then ask for help right here.</div>';
    return;
  }
  log.innerHTML = llmChatHistory.map(item => `
    <div class="llm-chat-msg ${item.role}">
      <div>${item.role === 'user' ? 'you' : 'local llm'}</div>
      <p>${escapeHtml(item.content)}</p>
    </div>
  `).join('');
  log.scrollTop = log.scrollHeight;
}

function setupLlmChat() {
  const form = $('llm-chat-form');
  const input = $('llm-chat-input');
  const status = $('llm-chat-status');
  const serverSelect = $('llm-chat-server');
  const reset = $('llm-reset');
  if (!form || !input || form.dataset.ready) return;
  form.dataset.ready = '1';
  serverSelect?.addEventListener('change', refreshLlmChatModels);
  reset?.addEventListener('click', async () => {
    llmChatHistory = [];
    renderLlmChatLog();
    if (status) status.textContent = 'resetting local LLM state...';
    const r = await postAction('/api/llm/reset');
    if (status) status.textContent = r.detail || 'local LLM state reset';
  });
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const message = input.value.trim();
    if (!message) return;
    if (!llmChatServers.length) {
      alert('No local LLM API is online. Start LM Studio or Ollama first.');
      return;
    }
    const btn = form.querySelector('button');
    input.value = '';
    llmChatHistory.push({ role: 'user', content: message });
    renderLlmChatLog();
    if (status) status.textContent = 'thinking...';
    if (btn) btn.disabled = true;
    try {
      const data = await postJson('/api/llm/chat', {
        message,
        history: llmChatHistory.slice(0, -1),
        port: $('llm-chat-server')?.value || '',
        model: $('llm-chat-model')?.value || '',
      });
      if (!data.ok) throw new Error(data.detail || 'Local LLM request failed');
      llmChatHistory.push({ role: 'assistant', content: data.reply });
      renderLlmChatLog();
      const tps = data.tokens_per_sec == null ? '' : ` · ${Number(data.tokens_per_sec).toFixed(1)} tok/s`;
      if (status) status.textContent = `${data.server} · ${data.model || 'default'} · ${data.elapsed_sec}s${tps}`;
    } catch (e) {
      llmChatHistory.push({ role: 'assistant', content: `Could not reach the local LLM: ${e.message || e}` });
      renderLlmChatLog();
      if (status) status.textContent = 'request failed';
    } finally {
      if (btn) btn.disabled = false;
      input.focus();
    }
  });
}

function renderGit(rows) {
  const tbl = $('git'); tbl.innerHTML = '';
  if (!rows.length) { tbl.innerHTML = '<tr><td class="no-data">No local repos found in config.json watched_repos. This does not mean GitHub watched repos.</td></tr>'; return; }
  const head = el('thead', {}, el('tr', {}, ...['repo', 'branch', 'state'].map(h => el('th', {}, h))));
  const body = el('tbody');
  rows.forEach(r => {
    const tr = el('tr', {});
    const state = [
      r.dirty ? '<span class="dot warn"></span>dirty' : '<span class="dot ok"></span>clean',
      r.ahead ? `↑${r.ahead}` : '',
      r.behind ? `↓${r.behind}` : '',
    ].filter(Boolean).join(' ');
    tr.innerHTML = `<td title="${r.path}">${r.name}</td><td>${r.branch || '-'}</td><td>${state}</td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(head); tbl.appendChild(body);
}

function renderToolchain(rows) {
  const tbl = $('toolchain'); tbl.innerHTML = '';
  if (!rows.length) return;
  const head = el('thead', {}, el('tr', {}, ...['tool', 'version', 'path'].map(h => el('th', {}, h))));
  const body = el('tbody');
  rows.forEach(r => {
    const tr = el('tr', {});
    tr.innerHTML = `<td>${r.tool}</td><td class="mono-clip">${r.version}</td><td class="mono-clip dim-path" title="${r.path}">${r.path}</td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(head); tbl.appendChild(body);
}

function renderInternet(inet) {
  const div = $('internet');
  div.innerHTML = Object.entries(inet).map(([k, v]) =>
    `<div class="metric-row"><span class="label"><span class="dot ${v ? 'ok' : 'bad'}"></span>${k}</span><span class="val">${v ? 'reachable' : 'down'}</span></div>`
  ).join('');
}

function renderAuth(rows) {
  const div = $('auth');
  if (!rows.length) { div.innerHTML = '<div class="no-data">checking...</div>'; return; }
  div.innerHTML = rows.map(r => {
    if (!r.installed) return `<div class="metric-row"><span class="label"><span class="dot dim"></span>${r.name}</span><span class="val kv">not installed</span></div>`;
    return `<div class="metric-row"><span class="label"><span class="dot ${r.ok ? 'ok' : 'bad'}"></span>${r.name}</span><span class="val">${r.ok ? 'logged in' : 'logged out'}</span></div>`;
  }).join('');
}

function renderOutdated(o) {
  const div = $('outdated');
  if (!o || !Object.keys(o).length) { div.innerHTML = '<div class="no-data">checking...</div>'; return; }
  div.innerHTML = Object.entries(o).map(([k, v]) => {
    if (v == null) return row(k, '-');
    const klass = v > 20 ? 'warn' : '';
    return row(k, `${v} packages`, klass);
  }).join('');
}

function renderPackages(rows) {
  lastPackages = rows;
  const tbl = $('packages'); tbl.innerHTML = '';
  const summary = $('package-summary');
  const outdatedCount = rows.filter(p => p.status === 'outdated').length;
  if (summary) {
    const managers = [...new Set(rows.map(p => p.manager))].filter(Boolean).join(' / ') || 'packages';
    summary.innerHTML = `
      <div class="package-summary-item"><strong>${rows.length || 0}</strong><span>installed</span></div>
      <div class="package-summary-item ${outdatedCount ? 'warn' : 'ok'}"><strong>${outdatedCount}</strong><span>updates</span></div>
      <div class="package-summary-copy">${escapeHtml(managers)}</div>
    `;
  }
  if (!rows.length) { tbl.innerHTML = '<tr><td class="no-data">checking package inventory...</td></tr>'; return; }
  const q = packageFilters.search.toLowerCase();
  const filtered = rows.filter(p => {
    if (packageFilters.manager !== 'all' && p.manager !== packageFilters.manager) return false;
    if (packageFilters.status !== 'all' && p.status !== packageFilters.status) return false;
    if (q && !`${p.manager} ${p.name} ${p.version || ''} ${p.latest || ''}`.toLowerCase().includes(q)) return false;
    return true;
  });
  if (!filtered.length) { tbl.innerHTML = '<tr><td class="no-data">no packages match the filters</td></tr>'; return; }
  const head = el('thead', {}, el('tr', {}, ...['', 'mgr', 'package', 'installed', 'latest', ''].map(h => el('th', {}, h))));
  const body = el('tbody');
  filtered.forEach(p => {
    const dot = p.status === 'outdated' ? 'warn' : p.status === 'current' ? 'ok' : 'dim';
    const label = p.status === 'outdated' ? 'update available' : p.status === 'current' ? 'current' : 'unknown';
    const tr = el('tr', {});
    if (p.status === 'outdated') tr.className = 'needs-update';
    const action = p.status === 'outdated' ? '<button class="btn-go pkg-update">⬆️ update</button>' : '';
    tr.innerHTML = `<td title="${label}"><span class="dot ${dot}"></span></td><td><span class="tag ${p.manager}">${p.manager}</span></td><td class="mono-clip" title="${p.name}">${p.name}</td><td class="mono-clip" title="${p.version || ''}">${p.version || '-'}</td><td class="mono-clip" title="${p.latest || ''}">${p.latest || '-'}</td><td>${action}</td>`;
    const btn = tr.querySelector('button');
    if (btn) {
      btn.addEventListener('click', () => {
        confirmAction('Update package?', `Run update for <b>${p.manager}</b> package <b>${p.name}</b>?<br><br>${p.version || '?'} → ${p.latest || 'latest'}`, async () => {
          btn.disabled = true;
          btn.textContent = '⬆️ updating';
          const r = await postJson('/api/packages/update', { manager: p.manager, name: p.name });
          btn.disabled = false;
          btn.textContent = '⬆️ update';
          const lines = [r.ok ? 'Update finished' : 'Update failed'];
          if (r.cmd) lines.push(`$ ${r.cmd}`);
          if (r.stdout) lines.push(r.stdout);
          if (r.stderr) lines.push(`stderr:\n${r.stderr}`);
          alert(lines.join('\n\n'));
        });
      });
    }
    body.appendChild(tr);
  });
  tbl.appendChild(head); tbl.appendChild(body);
}

async function renderCleanupCenter() {
  const div = $('cleanup-center');
  if (!div || div.dataset.ready) return;
  div.dataset.ready = '1';
  setupRecommendedCleanup();
  let actions = [
    { id: 'pip-cache', label: '🐍 pip cache' },
    { id: 'npm-cache', label: '📦 npm cache' },
    { id: 'brew-cache', label: '🍺 Homebrew cache' },
    { id: 'xcode-derived', label: '🛠 Xcode DerivedData' },
    { id: 'xcode-archives', label: '📚 Xcode Archives' },
    { id: 'sim-unavailable', label: '📱 Unavailable Simulators' },
    { id: 'user-caches', label: '🧹 User caches' },
    { id: 'playwright-cache', label: '🎭 Playwright browsers' },
    { id: 'bun-cache', label: '🥟 Bun cache' },
    { id: 'pnpm-store', label: '📦 pnpm store' },
    { id: 'yarn-cache', label: '🧶 Yarn cache' },
    { id: 'uv-cache', label: '⚡ uv cache' },
    { id: 'go-build-cache', label: '🐹 Go build cache' },
    { id: 'go-mod-cache', label: '🐹 Go module cache' },
    { id: 'docker-prune', label: '🐳 Docker prune' },
  ];
  try {
    const r = await fetch('/api/cleanup/actions');
    const data = await r.json();
    if (data.actions?.length) actions = data.actions;
  } catch {}
  div.innerHTML = '';
  actions.forEach(action => {
    const label = cleanupLabel(action);
    const btn = el('button', { class: 'cleanup-action' });
    setCleanupButtonPreview(btn, label, null);
    postJson('/api/cleanup/preview', { action: action.id })
      .then(preview => setCleanupButtonPreview(btn, label, preview))
      .catch(() => {});
    btn.addEventListener('click', () => {
      postJson('/api/cleanup/preview', { action: action.id }).then(preview => {
        const targets = (preview.targets || []).map(t => {
          const size = t.command ? '' : ` <span class="kv">${Number(t.gb || 0).toFixed(2)} GB</span>`;
          return `<div class="preview-row"><span>${escapeHtml(t.path)}</span>${size}</div>`;
        }).join('');
        const body = `<b>${escapeHtml(label)}</b><br><br>Estimated reclaim: <b>${Number(preview.estimated_gb || 0).toFixed(2)} GB</b><div class="preview-list">${targets}</div>`;
        confirmAction('Preview cleanup', body, async () => {
          btn.disabled = true;
          btn.textContent = '🧹 cleaning';
          const r = await postJson('/api/cleanup/run', { action: action.id });
          btn.disabled = false;
          setCleanupButtonPreview(btn, label, null);
          const lines = [r.title || 'Cleanup finished', r.detail || ''];
          if (r.removed_items) lines.push(`${r.removed_items} items removed`);
          if (r.errors?.length) lines.push(`Skipped: ${r.errors.join('; ')}`);
          if (r.stderr) lines.push(`stderr:\n${r.stderr}`);
          alert(lines.filter(Boolean).join('\n\n'));
          postJson('/api/cleanup/preview', { action: action.id })
            .then(next => setCleanupButtonPreview(btn, label, next))
            .catch(() => {});
        });
      }).catch(() => {
        alert('Could not preview cleanup action.');
      });
    });
    div.appendChild(btn);
  });
}

async function setupRecommendedCleanup() {
  const btn = $('cleanup-recommended');
  if (!btn || btn.dataset.ready) return;
  btn.dataset.ready = '1';
  const refresh = async () => {
    try {
      const r = await fetch('/api/cleanup/recommended');
      const data = await r.json();
      const mb = Number(data.estimated_gb || 0) * 1024;
      const hasWork = mb >= 10.24;
      btn.classList.toggle('cleanup-has-work', hasWork);
      btn.innerHTML = hasWork ? `✨ Recommended cleanup <span>${mb.toFixed(mb >= 100 ? 0 : 1)} MB</span>` : '✨ Recommended cleanup';
      btn.title = hasWork ? `${mb.toFixed(mb >= 100 ? 0 : 1)} MB across ${data.actions?.length || 0} cleanup targets` : 'No recommended cleanup right now';
    } catch {}
  };
  await refresh();
  btn.addEventListener('click', async () => {
    const preview = await fetch('/api/cleanup/recommended').then(r => r.json());
    const actions = preview.actions || [];
    if (!actions.length) {
      alert('No cleanup targets have measurable reclaimable space right now.');
      return;
    }
    const targets = actions.map(a => `<div class="preview-row"><span>${escapeHtml(cleanupLabel(a))}</span><span class="kv">${(Number(a.estimated_gb || 0) * 1024).toFixed(0)} MB</span></div>`).join('');
    confirmAction('Run recommended cleanup?', `sysdash will run <b>${actions.length}</b> cleanup action${actions.length === 1 ? '' : 's'} with measurable space.<div class="preview-list">${targets}</div>`, async () => {
      btn.disabled = true;
      btn.textContent = '🧹 cleaning';
      const r = await postJson('/api/cleanup/recommended/run', {});
      btn.disabled = false;
      await refresh();
      const lines = [r.title || 'Recommended cleanup finished', r.detail || ''];
      for (const item of r.results || []) {
        lines.push(`${item.title}: ${item.detail}`);
      }
      alert(lines.filter(Boolean).join('\n\n'));
      const cleanupDiv = $('cleanup-center');
      if (cleanupDiv) cleanupDiv.dataset.ready = '';
      renderCleanupCenter();
    });
  });
}

function setCleanupButtonPreview(btn, label, preview) {
  const gb = Number(preview?.estimated_gb || 0);
  const hasWork = gb >= 0.01;
  const mb = gb * 1024;
  btn.classList.toggle('cleanup-has-work', hasWork);
  btn.title = hasWork ? `${mb.toFixed(mb >= 100 ? 0 : 1)} MB can be cleared` : 'No measurable space to clear';
  btn.innerHTML = `<span class="cleanup-label">${escapeHtml(label)}</span>${hasWork ? `<span class="cleanup-size">${mb.toFixed(mb >= 100 ? 0 : 1)} MB</span>` : ''}`;
}

function cleanupLabel(action) {
  const icons = {
    'pip-cache': '🐍',
    'npm-cache': '📦',
    'brew-cache': '🍺',
    'xcode-derived': '🛠',
    'xcode-archives': '📚',
    'sim-unavailable': '📱',
    'user-caches': '🧹',
    'playwright-cache': '🎭',
    'bun-cache': '🥟',
    'pnpm-store': '📦',
    'yarn-cache': '🧶',
    'uv-cache': '⚡',
    'go-build-cache': '🐹',
    'go-mod-cache': '🐹',
    'docker-prune': '🐳',
  };
  const label = action.label || action.id;
  return label.match(/\p{Emoji}/u) ? label : `${icons[action.id] || '🧹'} ${label}`;
}

function renderSshVpn(s) {
  const div = $('sshvpn');
  let html = '';
  html += `<div class="kv">🔐 SSH:</div>`;
  if (!s.ssh?.length) html += '<div class="no-data">none</div>';
  else html += s.ssh.map(c => `<div class="metric-row"><span class="label">${c.process}</span><span class="val">${c.remote}</span></div>`).join('');
  html += `<div class="kv" style="margin-top:6px">🛡 VPN:</div>`;
  if (!s.vpn?.length) html += '<div class="no-data">none</div>';
  else html += s.vpn.map(v => `<div class="metric-row"><span class="label">${v.name}</span><span class="val">${v.ip}</span></div>`).join('');
  div.innerHTML = html;
}

function renderLogs(rows) {
  const div = $('logs');
  if (!rows.length) { div.innerHTML = '<div class="no-data">no recent error lines (configure log_files in config.json)</div>'; return; }
  div.innerHTML = rows.map(r => {
    const fname = r.file.split('/').pop();
    return `<div class="log-line"><span class="file">${fname}</span>${r.line}</div>`;
  }).join('');
}

function renderBattery(bt) {
  const div = $('battery');
  const top = $('header-battery');
  if (!bt || (!bt.battery && !bt.temperature_c)) {
    if (top) top.textContent = '🔋 --';
    if (div) div.innerHTML = '<div class="no-data">no data</div>';
    return;
  }
  let html = '';
  if (bt.battery) {
    html += row('battery', `${bt.battery.pct}% ${bt.battery.plugged ? '⚡' : ''}`);
    if (top) top.textContent = `🔋 ${bt.battery.pct}%${bt.battery.plugged ? ' ⚡' : ''}`;
  }
  if (bt.temperature_c) {
    html += row(bt.temperature_label || 'temp', `${bt.temperature_c}°C`);
  }
  if (div) div.innerHTML = html;
}

function renderCheats(rows) {
  lastCheats = rows;
  setupCommandBuilder(rows);
  const div = $('cheats');
  const options = $('terminal-commands');
  if (options) {
    options.innerHTML = rows.map(c => `<option value="${escapeHtml(c.cmd)}">${escapeHtml(c.desc)}</option>`).join('');
  }
  if (!div) return;
  div.innerHTML = '';
  rows.forEach(c => {
    const row = el('div', { class: 'cheat-row', title: 'click to run in Terminal' });
    row.innerHTML = `<code>⚡ ${c.cmd}</code><span class="desc">${c.desc}</span>`;
    row.addEventListener('click', async () => {
      row.classList.add('running');
      try {
        const r = await postJson('/api/cheats/run', { cmd: c.cmd });
        if (!r.ok) alert(`Could not run command:\n\n${r.stderr || JSON.stringify(r)}`);
      } catch (e) {
        alert(`Could not run command:\n\n${e}`);
      } finally {
        setTimeout(() => row.classList.remove('running'), 450);
      }
    });
    div.appendChild(row);
  });
}

function renderDiagnostic(findings) {
  const div = $('diagnostic');
  if (!findings.length) { div.innerHTML = '<div class="no-data">no findings</div>'; return; }
  div.innerHTML = '';
  findings.forEach(f => {
    const row = el('div', { class: 'diag-finding' });
    const text = el('span', {}, f);
    row.appendChild(text);
    if (!f.startsWith('All clear.')) {
      const btn = el('button', { class: 'btn-go resolve-btn', title: 'Run the safe resolver for this finding' }, '🛠 resolve');
      btn.addEventListener('click', () => {
        confirmAction('Resolve issue?', `${escapeHtml(f)}<br><br>sysdash will run its safe resolver for this finding.`, async () => {
          btn.disabled = true;
          btn.textContent = '🛠 resolving';
          const result = await postJson('/api/resolve', { finding: f });
          btn.disabled = false;
          btn.textContent = '🛠 resolve';
          const lines = [result.title || 'Resolve result', result.detail || ''];
          if (result.errors?.length) lines.push(`Skipped: ${result.errors.join('; ')}`);
          alert(lines.filter(Boolean).join('\n\n'));
        });
      });
      row.appendChild(btn);
    }
    div.appendChild(row);
  });
}

function renderDiskHogs(rows) {
  const tbl = $('disk-hogs'); tbl.innerHTML = '';
  if (!rows.length) { tbl.innerHTML = '<tr><td class="no-data">scanning...</td></tr>'; return; }
  const head = el('thead', {}, el('tr', {}, ...['target', 'GB'].map(h => el('th', {}, h))));
  const body = el('tbody');
  rows.forEach(r => {
    const tr = el('tr', {});
    const klass = r.gb >= 5 ? 'bad' : r.gb >= 1 ? 'warn' : '';
    tr.innerHTML = `<td>${r.label}</td><td class="${klass}">${r.gb ?? '-'}</td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(head); tbl.appendChild(body);
}

function renderDataHogs(rows) {
  const tbl = $('data-hogs');
  if (!tbl) return;
  tbl.innerHTML = '';
  if (!rows.length) {
    tbl.innerHTML = '<tr><td class="no-data">waiting for scan...</td></tr>';
    return;
  }
  const head = el('thead', {}, el('tr', {}, ...['file', 'folder', 'GB', ''].map(h => el('th', {}, h))));
  const body = el('tbody');
  rows.slice(0, 5).forEach((r, idx) => {
    const gb = Number(r.gb || 0);
    const klass = gb >= 5 ? 'bad' : gb >= 1 ? 'warn' : '';
    const tr = el('tr', {});
    tr.innerHTML = `
      <td class="mono-clip data-file" title="${escapeHtml(r.path || '')}">${escapeHtml(r.name || r.path || '-')}</td>
      <td class="mono-clip dim-path data-dir" title="${escapeHtml(r.dir || '')}">${escapeHtml(r.dir || '-')}</td>
      <td class="${klass}">${gb ? gb.toFixed(2) : '-'}</td>
      <td><button class="btn-x data-delete" data-path="${escapeHtml(r.path || '')}" data-name="${escapeHtml(r.name || 'file')}" data-idx="${idx}">trash</button></td>`;
    body.appendChild(tr);
  });
  tbl.appendChild(head);
  tbl.appendChild(body);
  tbl.querySelectorAll('.data-delete').forEach(btn => {
    btn.addEventListener('click', async () => {
      const path = btn.dataset.path || '';
      const name = btn.dataset.name || 'this file';
      const row = rows[Number(btn.dataset.idx || 0)] || {};
      const size = row.gb ? `${Number(row.gb).toFixed(2)} GB` : 'unknown size';
      confirmAction('Move file to Trash?', `<b>${escapeHtml(name)}</b><br><br>${escapeHtml(path)}<br><br>Size: <b>${size}</b>`, async () => {
        btn.disabled = true;
        btn.textContent = 'moving';
        try {
          const data = await postJson('/api/data-hogs/delete', { path });
          renderDataHogs(data.data_hogs || []);
          alert(data.detail || 'Moved to Trash.');
        } catch (e) {
          btn.disabled = false;
          btn.textContent = 'trash';
          alert(`Could not move file to Trash:\n\n${e}`);
        }
      });
    });
  });
}

function setupDataHogsControls() {
  const btn = $('data-hogs-refresh');
  if (!btn || btn.dataset.ready) return;
  btn.dataset.ready = '1';
  btn.addEventListener('click', async () => {
    const tbl = $('data-hogs');
    btn.disabled = true;
    btn.textContent = 'scanning...';
    if (tbl) tbl.innerHTML = '<tr><td class="no-data">scanning /System/Volumes/Data...</td></tr>';
    try {
      const data = await postJson('/api/data-hogs/refresh', {});
      renderDataHogs(data.data_hogs || []);
    } catch (e) {
      if (tbl) tbl.innerHTML = '<tr><td class="no-data">scan failed</td></tr>';
    } finally {
      btn.disabled = false;
      btn.textContent = '↻ refresh';
    }
  });
}

// ---- UI polish ----
const CARD_KIND_MAP = [
  ['top processes', 'process', '⚙️'],
  ['cpu', 'cpu', '🧠'],
  ['ram', 'ram', '🧩'],
  ['sysdash footprint', 'sysdash', '🛰'],
  ['diagnostic', 'diagnostic', '🩺'],
  ['port', 'ports', '🔌'],
  ['docker', 'docker', '🐳'],
  ['service', 'services', '🧰'],
  ['cleanup', 'disk', '🧹'],
  ['data volume', 'disk', '💽'],
  ['disk', 'disk', '💾'],
  ['network', 'network', '📡'],
  ['terminal', 'terminal', '💻'],
  ['llm assistant', 'llm', '🤖'],
  ['app shortcuts', 'shortcut', '🖱'],
  ['launcher', 'launcher', '🚀'],
  ['internet', 'internet', '🌐'],
  ['auth', 'auth', '🔐'],
  ['toolchain', 'toolchain', '🛠'],
  ['runtime', 'toolchain', '⚙️'],
  ['local llm', 'llm', '🤖'],
  ['git', 'toolchain', '🌿'],
  ['package', 'packages', '📦'],
  ['ssh', 'network', '🛡'],
  ['log', 'logs', '📜'],
  ['battery', 'battery', '🔋'],
  ['command builder', 'cheats', '🧱'],
  ['thermal', 'battery', '🌡'],
  ['cheatsheet', 'cheats', '⚡'],
];

const LEARN_NOTES = {
  cpu: 'CPU is the computer doing work. High CPU is normal during builds, but constant high CPU can make everything feel slow.',
  ram: 'RAM is short-term memory. On macOS, memory pressure matters more than percent used.',
  process: 'Processes are running apps and helper tasks. Killing one can close an app or stop a server.',
  diagnostic: 'Diagnostics summarize the most important problems first and offer safe fixes when sysdash has one.',
  sysdash: 'This shows the dashboard itself, so you can tell if sysdash is becoming part of the problem.',
  ports: 'Ports are numbered doors for local servers. A busy port can stop a new dev server from starting.',
  docker: 'Docker containers are isolated services. Stopped containers can still take disk space.',
  services: 'Homebrew services are background programs like databases or queues.',
  disk: 'Disk space problems often come from caches, simulators, Docker images, and build folders.',
  network: 'Network panels show both internet reachability and local connections.',
  terminal: 'This terminal only runs sysdash-approved commands so it stays safe.',
  shortcut: 'App Shortcuts are one-click launchers for trusted local files like .command scripts and .app bundles.',
  launcher: 'Dev Launcher stores project commands and opens them in Terminal from the right folder.',
  internet: 'These checks tell you whether basic internet, DNS, and GitHub access are working.',
  auth: 'Auth status catches expired CLI logins before commands fail.',
  toolchain: 'Toolchain versions explain which node/python/git/etc your shell is actually using.',
  llm: 'Local LLM monitoring checks model servers, model counts, and memory used by local inference processes.',
  packages: 'Package status shows installed versions and whether updates are available.',
  logs: 'Recent errors show useful clues from configured log files.',
  battery: 'Battery and thermals can explain slowdowns on laptops.',
  cheats: 'Command Builder and Cheatsheet give safe terminal commands for common debugging tasks.',
};

function cardId(card, index) {
  const title = card.querySelector('h2')?.textContent || `card-${index}`;
  return title.toLowerCase().replace(/\?/g, '').trim().replace(/[^a-z0-9]+/g, '-');
}

function setupTheme() {
  const saved = localStorage.getItem(THEME_STORE) || 'auto';
  const apply = choice => {
    const systemLight = window.matchMedia?.('(prefers-color-scheme: light)').matches;
    document.body.dataset.theme = choice === 'auto' ? (systemLight ? 'light' : 'dark') : choice;
    document.querySelectorAll('.theme-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.themeChoice === choice));
  };
  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      localStorage.setItem(THEME_STORE, btn.dataset.themeChoice);
      apply(btn.dataset.themeChoice);
    });
  });
  window.matchMedia?.('(prefers-color-scheme: light)').addEventListener?.('change', () => apply(localStorage.getItem(THEME_STORE) || 'auto'));
  apply(saved);
}

function setupCards() {
  document.querySelectorAll('.card').forEach((card, index) => {
    const h2 = card.querySelector('h2');
    if (!h2 || h2.dataset.enhanced) return;
    const text = h2.textContent.toLowerCase();
    const match = CARD_KIND_MAP.find(([needle]) => text.includes(needle)) || ['card', 'default', 'SYS'];
    card.dataset.kind = match[1];
    const id = cardId(card, index);
    card.dataset.cardId = id;
    if (collapsedCards.has(id)) card.classList.add('is-collapsed');
    if (pinnedCards.has(id)) card.classList.add('is-pinned');
    h2.insertBefore(el('span', { class: 'card-icon', 'aria-hidden': 'true' }, match[2]), h2.firstChild);
    const note = LEARN_NOTES[match[1]];
    if (note) h2.after(el('div', { class: 'learn-note' }, note));
    const pin = el('button', { class: 'card-control pin-toggle', title: 'Pin card to top', type: 'button' }, '📌');
    pin.addEventListener('click', ev => {
      ev.stopPropagation();
      card.classList.toggle('is-pinned');
      if (card.classList.contains('is-pinned')) pinnedCards.add(id);
      else pinnedCards.delete(id);
      localStorage.setItem(PINNED_CARDS_STORE, JSON.stringify(Array.from(pinnedCards)));
      sortColumn(card.closest('.col'));
    });
    const up = el('button', { class: 'card-control', title: 'Move card up', type: 'button' }, '↑');
    up.addEventListener('click', ev => {
      ev.stopPropagation();
      moveCard(card, -1);
    });
    const down = el('button', { class: 'card-control', title: 'Move card down', type: 'button' }, '↓');
    down.addEventListener('click', ev => {
      ev.stopPropagation();
      moveCard(card, 1);
    });
    const left = el('button', { class: 'card-control', title: 'Move card to left column', type: 'button' }, '←');
    left.addEventListener('click', ev => {
      ev.stopPropagation();
      moveCardColumn(card, -1);
    });
    const right = el('button', { class: 'card-control', title: 'Move card to right column', type: 'button' }, '→');
    right.addEventListener('click', ev => {
      ev.stopPropagation();
      moveCardColumn(card, 1);
    });
    const toggle = el('button', { class: 'collapse-toggle', title: 'Collapse section', type: 'button' }, '⌄');
    toggle.addEventListener('click', ev => {
      ev.stopPropagation();
      card.classList.toggle('is-collapsed');
      if (card.classList.contains('is-collapsed')) collapsedCards.add(id);
      else collapsedCards.delete(id);
      localStorage.setItem(COLLAPSE_STORE, JSON.stringify(Array.from(collapsedCards)));
    });
    const controls = el('span', { class: 'card-controls' }, pin, left, up, down, right, toggle);
    h2.appendChild(controls);
    h2.dataset.enhanced = '1';
  });
  applyCardOrder();
}

function setupLearningMode() {
  const btn = $('learning-mode');
  const apply = enabled => {
    document.body.classList.toggle('learning-on', enabled);
    if (btn) {
      btn.classList.toggle('active', enabled);
      btn.textContent = enabled ? '🎓 learning on' : '🎓 learn';
    }
  };
  const saved = localStorage.getItem(LEARNING_STORE) === '1';
  apply(saved);
  btn?.addEventListener('click', () => {
    const enabled = !document.body.classList.contains('learning-on');
    localStorage.setItem(LEARNING_STORE, enabled ? '1' : '0');
    apply(enabled);
  });
}

function cardOrderKey(col) {
  return `col-${Array.from(document.querySelectorAll('.col')).indexOf(col)}`;
}

function storedCardOrder() {
  try { return JSON.parse(localStorage.getItem(CARD_ORDER_STORE) || '{}'); }
  catch { return {}; }
}

function saveCardOrder() {
  const order = {};
  document.querySelectorAll('.col').forEach(col => {
    order[cardOrderKey(col)] = Array.from(col.querySelectorAll('.card')).map(card => card.dataset.cardId);
  });
  localStorage.setItem(CARD_ORDER_STORE, JSON.stringify(order));
}

function applyCardOrder() {
  const order = storedCardOrder();
  const allCards = Array.from(document.querySelectorAll('.card'));
  document.querySelectorAll('.col').forEach(col => {
    const ids = order[cardOrderKey(col)] || [];
    ids.forEach(id => {
      const card = allCards.find(c => c.dataset.cardId === id);
      if (card) col.appendChild(card);
    });
    sortColumn(col, false);
  });
}

function sortColumn(col, persist = true) {
  if (!col) return;
  const cards = Array.from(col.querySelectorAll('.card'));
  const pinned = cards.filter(card => card.classList.contains('is-pinned'));
  const normal = cards.filter(card => !card.classList.contains('is-pinned'));
  [...pinned, ...normal].forEach(card => col.appendChild(card));
  if (persist) saveCardOrder();
}

function moveCard(card, direction) {
  const col = card.closest('.col');
  if (!col) return;
  const cards = Array.from(col.querySelectorAll('.card'));
  const idx = cards.indexOf(card);
  const swap = cards[idx + direction];
  if (!swap) return;
  if (direction < 0) col.insertBefore(card, swap);
  else col.insertBefore(swap, card);
  saveCardOrder();
}

function moveCardColumn(card, direction) {
  const cols = Array.from(document.querySelectorAll('.col'));
  const current = card.closest('.col');
  const idx = cols.indexOf(current);
  const target = cols[idx + direction];
  if (!target) return;
  target.appendChild(card);
  sortColumn(target, false);
  sortColumn(current, false);
  saveCardOrder();
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function setupDock() {
  document.querySelectorAll('#dock button').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = $(btn.dataset.scrollTarget);
      const card = target?.closest('.card') || target;
      if (!card) return;
      card.classList?.remove('is-collapsed');
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  });
}

function setupCommandCenterNav() {
  const targets = {
    cpu: 'cpu-block',
    ram: 'ram-block',
    swap: 'ram-block',
    net: 'net-block',
    ports: 'ports',
    updates: 'packages',
    alerts: 'diagnostic',
  };
  document.querySelectorAll('#command-center .cc-item').forEach(item => {
    const id = targets[item.dataset.cc];
    if (!id) return;
    item.setAttribute('role', 'button');
    item.setAttribute('tabindex', '0');
    item.title = 'Jump to details';
    const jump = () => {
      const target = $(id);
      const card = target?.closest('.card') || target;
      if (!card) return;
      card.classList?.remove('is-collapsed');
      card.classList?.add('focus-flash');
      setTimeout(() => card.classList?.remove('focus-flash'), 900);
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
    item.addEventListener('click', jump);
    item.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        jump();
      }
    });
  });
}

function setupCommandPalette() {
  const bg = $('palette-bg');
  const input = $('palette-input');
  const results = $('palette-results');
  if (!bg || !input || !results) return;

  const close = () => {
    bg.classList.remove('show');
    bg.setAttribute('aria-hidden', 'true');
  };
  const open = () => {
    renderPalette('');
    bg.classList.add('show');
    bg.setAttribute('aria-hidden', 'false');
    input.value = '';
    setTimeout(() => input.focus(), 0);
  };
  const jump = id => {
    const target = $(id);
    const card = target?.closest('.card') || target;
    if (!card) return;
    card.classList.remove('is-collapsed');
    card.classList.add('focus-flash');
    setTimeout(() => card.classList.remove('focus-flash'), 900);
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  };
  const actions = () => [
    { label: '🩺 Jump to Diagnostic', tags: 'diagnostic health alerts', run: () => jump('diagnostic') },
    { label: '🧹 Run recommended cleanup', tags: 'cleanup disk cache recommended', run: () => $('cleanup-recommended')?.click() },
    { label: '💻 Jump to Terminal', tags: 'terminal command shell', run: () => jump('terminal-output') },
    { label: '🚀 Jump to Dev Launcher', tags: 'launcher projects apps', run: () => jump('launcher-saved') },
    { label: '🖱 Jump to App Shortcuts', tags: 'shortcuts command apps scripts launch', run: () => jump('shortcut-saved') },
    { label: '🤖 Jump to Local LLMs', tags: 'llm lm studio ollama llama models ai', run: () => jump('llms') },
    { label: '🔌 Jump to Ports', tags: 'ports listening network', run: () => jump('ports') },
    { label: '📦 Jump to Packages', tags: 'packages updates', run: () => jump('packages') },
    { label: '🧱 Jump to Command Builder', tags: 'builder command beginner', run: () => jump('builder-preview') },
    { label: '⚙️ Open Settings', tags: 'settings config preferences', run: () => $('open-settings')?.click() },
    ...lastCheats.slice(0, 40).map(c => ({
      label: `⚡ ${c.desc}`,
      tags: `${c.cmd} ${c.desc}`,
      meta: c.cmd,
      run: async () => {
        const r = await postJson('/api/cheats/run', { cmd: c.cmd });
        if (!r.ok) alert(`Could not run command:\n\n${r.stderr || JSON.stringify(r)}`);
      },
    })),
  ];
  const renderPalette = q => {
    const query = q.trim().toLowerCase();
    const rows = actions().filter(a => !query || `${a.label} ${a.tags} ${a.meta || ''}`.toLowerCase().includes(query)).slice(0, 12);
    results.innerHTML = rows.map((a, i) => `<button class="palette-row ${i === 0 ? 'active' : ''}" data-index="${i}"><span>${escapeHtml(a.label)}</span>${a.meta ? `<code>${escapeHtml(a.meta)}</code>` : ''}</button>`).join('');
    results.querySelectorAll('.palette-row').forEach(btn => {
      btn.addEventListener('click', async () => {
        const action = rows[Number(btn.dataset.index)];
        close();
        await action?.run();
      });
    });
  };
  input.addEventListener('input', () => renderPalette(input.value));
  input.addEventListener('keydown', async ev => {
    const buttons = Array.from(results.querySelectorAll('.palette-row'));
    const current = buttons.findIndex(b => b.classList.contains('active'));
    if (ev.key === 'Escape') close();
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      const next = ev.key === 'ArrowDown' ? Math.min(buttons.length - 1, current + 1) : Math.max(0, current - 1);
      buttons.forEach((b, i) => b.classList.toggle('active', i === next));
    }
    if (ev.key === 'Enter') {
      ev.preventDefault();
      const active = results.querySelector('.palette-row.active') || results.querySelector('.palette-row');
      active?.click();
    }
  });
  bg.addEventListener('click', ev => { if (ev.target === bg) close(); });
  document.addEventListener('keydown', ev => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'k') {
      ev.preventDefault();
      open();
    }
  });
}

function setupPackageControls() {
  const search = $('pkg-search');
  const manager = $('pkg-manager');
  const status = $('pkg-status');
  const updateAll = $('pkg-update-all');
  if (!search || !manager || !status || !updateAll) return;
  const rerender = () => {
    packageFilters = {
      search: search.value.trim(),
      manager: manager.value,
      status: status.value,
    };
    renderPackages(lastPackages);
  };
  search.addEventListener('input', rerender);
  manager.addEventListener('change', rerender);
  status.addEventListener('change', rerender);
  updateAll.addEventListener('click', () => {
    const selected = lastPackages.filter(p => {
      if (p.status !== 'outdated') return false;
      if (packageFilters.manager !== 'all' && p.manager !== packageFilters.manager) return false;
      if (packageFilters.status !== 'all' && packageFilters.status !== 'outdated') return false;
      const q = packageFilters.search.toLowerCase();
      if (q && !`${p.manager} ${p.name} ${p.version || ''} ${p.latest || ''}`.toLowerCase().includes(q)) return false;
      return true;
    });
    if (!selected.length) {
      alert('No outdated packages match the current filters.');
      return;
    }
    confirmAction('Update filtered packages?', `Update <b>${selected.length}</b> outdated package${selected.length === 1 ? '' : 's'} matching the current filters?`, async () => {
      updateAll.disabled = true;
      updateAll.textContent = '⬆️ updating';
      const r = await postJson('/api/packages/update-all', {
        managers: [...new Set(selected.map(p => p.manager))],
        names: selected.map(p => p.name),
      });
      updateAll.disabled = false;
      updateAll.textContent = '⬆️ update filtered';
      const failed = (r.results || []).filter(x => !x.ok);
      const lines = [`Updated ${r.count || 0} package${r.count === 1 ? '' : 's'}.`];
      if (failed.length) lines.push(`Failed: ${failed.map(x => `${x.manager}/${x.name}`).join(', ')}`);
      alert(lines.join('\n\n'));
    });
  });
}

const BUILDER_GROUPS = [
  {
    label: 'Disk space',
    help: 'Find what is using storage and preview safe cleanup targets.',
    commands: ['df -h', 'du -sh ~/* 2>/dev/null | sort -h | tail -20', 'du -sh ~/Library/Caches ~/.cache 2>/dev/null', 'brew cleanup --dry-run', 'docker system df'],
  },
  {
    label: 'Ports and local servers',
    help: 'See which local servers are listening and what owns each port.',
    commands: ['lsof -nP -iTCP -sTCP:LISTEN', "netstat -vanp tcp | grep LISTEN", 'curl -s http://127.0.0.1:55067/api/snapshot | python3 -m json.tool | head -80'],
  },
  {
    label: 'Performance',
    help: 'Inspect CPU, RAM, load average, and memory pressure.',
    commands: ['ps aux | sort -nrk 3,3 | head -15', 'ps aux | sort -nrk 4,4 | head -15', 'vm_stat', 'memory_pressure', 'uptime'],
  },
  {
    label: 'Network',
    help: 'Check internet, DNS, HTTPS, and network interface state.',
    commands: ['ping -c 4 1.1.1.1', 'dig github.com', 'curl -I https://github.com', "ifconfig | grep '^[a-z].*:'", 'scutil --dns | head -60'],
  },
  {
    label: 'Dev tools',
    help: 'Check versions and health for common development tools.',
    commands: ['python3 --version', 'node --version && npm --version', 'brew doctor', 'brew services list', 'xcode-select -p'],
  },
  {
    label: 'sysdash',
    help: 'Inspect sysdash itself: port, logs, errors, launch agent, and recent git state.',
    commands: ['cat ~/.sysdash-port', 'tail -n 80 ~/sysdash/sysdash.log', 'tail -n 80 ~/sysdash/sysdash.err', 'git -C ~/sysdash status --short', 'launchctl print gui/$(id -u)/com.sysdash.agent | head -80'],
  },
];

let commandBuilderReady = false;

function setupCommandBuilder(rows = lastCheats) {
  const goal = $('builder-goal');
  const command = $('builder-command');
  const preview = $('builder-preview');
  if (!goal || !command || !preview || !rows.length) return;
  const allowed = new Map(rows.map(r => [r.cmd, r.desc]));
  if (!commandBuilderReady) {
    goal.innerHTML = BUILDER_GROUPS.map((g, i) => `<option value="${i}">${escapeHtml(g.label)}</option>`).join('');
    goal.addEventListener('change', updateCommandBuilder);
    command.addEventListener('change', updateBuilderPreview);
    $('builder-inline')?.addEventListener('click', () => runBuilderCommand(false));
    $('builder-terminal')?.addEventListener('click', () => runBuilderCommand(true));
    commandBuilderReady = true;
  }
  updateCommandBuilder();

  function updateCommandBuilder() {
    const group = BUILDER_GROUPS[Number(goal.value || 0)] || BUILDER_GROUPS[0];
    const cmds = group.commands.filter(cmd => allowed.has(cmd));
    command.innerHTML = cmds.map(cmd => `<option value="${escapeHtml(cmd)}">${escapeHtml(allowed.get(cmd) || cmd)}</option>`).join('');
    updateBuilderPreview();
  }

  function updateBuilderPreview() {
    const group = BUILDER_GROUPS[Number(goal.value || 0)] || BUILDER_GROUPS[0];
    const cmd = command.value;
    const desc = allowed.get(cmd) || 'Choose a command.';
    preview.innerHTML = `<div>${escapeHtml(group.help)}</div><code>${escapeHtml(cmd || '')}</code><span>${escapeHtml(desc)}</span>`;
  }
}

async function runBuilderCommand(openTerminal) {
  const command = $('builder-command');
  const preview = $('builder-preview');
  const cmd = command?.value || '';
  if (!cmd) return;
  preview.innerHTML = `<div>Running safe command...</div><code>${escapeHtml(cmd)}</code>`;
  const endpoint = openTerminal ? '/api/cheats/run' : '/api/terminal/run';
  const r = await postJson(endpoint, { cmd });
  if (openTerminal) {
    preview.innerHTML = `<div>${r.ok ? 'Opened in Terminal.' : 'Could not open Terminal.'}</div><code>${escapeHtml(cmd)}</code><span>${escapeHtml(r.stderr || '')}</span>`;
    return;
  }
  const output = [r.stdout, r.stderr ? `stderr:\n${r.stderr}` : '', !r.ok ? `exit: ${r.returncode ?? 'blocked'}` : ''].filter(Boolean).join('\n\n');
  preview.innerHTML = `<div>${r.ok ? 'Command finished.' : 'Command did not finish cleanly.'}</div><code>${escapeHtml(cmd)}</code><pre>${escapeHtml(output || 'done')}</pre>`;
}

async function loadSettings() {
  const panel = $('settings-panel');
  if (!panel) return;
  const cfg = await fetch('/api/config').then(r => r.json());
  activeConfig = cfg;
  const thresholds = cfg.alert_thresholds || {};
  const flags = cfg.feature_flags || {};
  setInput('set-port', cfg.port || 55067);
  setInput('set-cpu', thresholds.cpu_pct ?? 90);
  setInput('set-ram', thresholds.ram_pct ?? 85);
  setInput('set-disk', thresholds.disk_free_pct ?? 10);
  setInput('set-outdated-interval', cfg.outdated_check_interval_sec ?? 600);
  setInput('set-package-interval', cfg.package_inventory_interval_sec ?? 900);
  setInput('set-repos', (cfg.watched_repos || []).join('\n'));
  setInput('set-logs', (cfg.log_files || []).join('\n'));
  setChecked('set-red-pressure', thresholds.memory_pressure_red ?? true);
  setChecked('set-battery', flags.show_battery ?? true);
  setChecked('set-auth', flags.show_auth_status ?? true);
  setChecked('set-disk-hogs', flags.show_disk_hogs ?? true);
}

function collectSettings() {
  return {
    ...(activeConfig || {}),
    port: Number($('set-port').value || 55067),
    watched_repos: linesFromTextarea('set-repos'),
    log_files: linesFromTextarea('set-logs'),
    outdated_check_interval_sec: Number($('set-outdated-interval').value || 600),
    package_inventory_interval_sec: Number($('set-package-interval').value || 900),
    alert_thresholds: {
      ...((activeConfig || {}).alert_thresholds || {}),
      cpu_pct: Number($('set-cpu').value || 90),
      ram_pct: Number($('set-ram').value || 85),
      disk_free_pct: Number($('set-disk').value || 10),
      memory_pressure_red: $('set-red-pressure').checked,
    },
    feature_flags: {
      ...((activeConfig || {}).feature_flags || {}),
      show_battery: $('set-battery').checked,
      show_auth_status: $('set-auth').checked,
      show_disk_hogs: $('set-disk-hogs').checked,
    },
  };
}

function openSettings() {
  const panel = $('settings-panel');
  if (!panel) return;
  panel.classList.add('show');
  panel.setAttribute('aria-hidden', 'false');
  loadSettings().catch(e => alert(`Could not load settings:\n\n${e}`));
}

function closeSettings() {
  const panel = $('settings-panel');
  if (!panel) return;
  panel.classList.remove('show');
  panel.setAttribute('aria-hidden', 'true');
}

function setupSettingsPanel() {
  $('open-settings')?.addEventListener('click', openSettings);
  $('close-settings')?.addEventListener('click', closeSettings);
  $('reload-settings')?.addEventListener('click', () => loadSettings());
  $('settings-panel')?.addEventListener('click', ev => {
    if (ev.target === $('settings-panel')) closeSettings();
  });
  $('save-settings')?.addEventListener('click', async () => {
    const btn = $('save-settings');
    btn.disabled = true;
      btn.textContent = '💾 saving';
    try {
      const result = await postJson('/api/config', collectSettings());
      activeConfig = result.config;
      alert(result.detail || 'Settings saved.');
      closeSettings();
    } catch (e) {
      alert(`Could not save settings:\n\n${e}`);
    } finally {
      btn.disabled = false;
      btn.textContent = '💾 save settings';
    }
  });
}

setupTheme();
setupCards();
setupLearningMode();
setupDock();
setupCommandCenterNav();
setupCommandPalette();
setupPackageControls();
setupSettingsPanel();
setupDataHogsControls();
setupLlmChat();

$('modal-cancel')?.addEventListener('click', () => {
  $('modal-bg')?.classList.remove('show');
  $('modal')?.classList.remove('process-modal');
  $('modal')?.classList.remove('shortcut-modal');
  if ($('modal-confirm')) $('modal-confirm').style.display = '';
});

// ---- WS ----
let ws;
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => $('ws-status').textContent = 'connected';
  ws.onclose = () => {
    $('ws-status').textContent = 'disconnected, retrying...';
    setTimeout(connect, 1500);
  };
  ws.onerror = () => $('ws-status').textContent = 'error';
  ws.onmessage = (ev) => {
    try { render(JSON.parse(ev.data)); }
    catch (e) { console.error(e); }
  };
}
connect();

$('run-diag').addEventListener('click', async () => {
  const r = await fetch('/api/diagnostic', { method: 'POST' });
  const data = await r.json();
  alert((data.findings || []).join('\n\n'));
});

$('terminal-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const input = $('terminal-input');
  const output = $('terminal-output');
  const cmd = input.value.trim();
  if (!cmd) return;
  output.textContent = `$ ${cmd}\n\nrunning...`;
  const r = await postJson('/api/terminal/run', { cmd });
  const chunks = [`$ ${cmd}`];
  if (r.stdout) chunks.push(r.stdout);
  if (r.stderr) chunks.push(`stderr:\n${r.stderr}`);
  if (!r.ok) chunks.push(`exit: ${r.returncode ?? 'blocked'}`);
  output.textContent = chunks.join('\n\n') || 'done';
});

// ---- App Shortcuts ----
const SHORTCUT_STORE = 'sysdash_app_shortcuts_v1';
let appShortcuts = JSON.parse(localStorage.getItem(SHORTCUT_STORE) || '[]');

function shortcutUid() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

function shortcutIcon(item) {
  const ext = (item.ext || item.path?.split('.').pop() || '').toLowerCase().replace(/^\./, '');
  if (ext === 'command' || ext === 'terminal') return '⌘';
  if (ext === 'app') return '📱';
  if (ext === 'workflow' || ext === 'scpt') return '⚙️';
  if (['sh', 'py', 'js'].includes(ext)) return '📜';
  if (['html', 'webloc', 'url'].includes(ext)) return '🌐';
  return '🖱';
}

function saveShortcuts() {
  localStorage.setItem(SHORTCUT_STORE, JSON.stringify(appShortcuts));
}

function shortcutRow(item, mode) {
  const row = el('div', { class: 'shortcut-row' });
  row.innerHTML = `
    <button class="shortcut-launch" title="Run shortcut">
      <span class="launcher-emoji">${shortcutIcon(item)}</span>
      <span class="launcher-name">${escapeHtml(item.name || 'Shortcut')}</span>
    </button>
    <span class="shortcut-ext">${escapeHtml(item.ext || '')}</span>
    <button class="btn-x shortcut-remove" title="Remove shortcut">×</button>
  `;
  const launch = row.querySelector('.shortcut-launch');
  const sync = () => {
    item.name = item.name?.trim() || 'Shortcut';
    item.path = item.path?.trim() || '';
    item.ext = item.path.includes('.') ? `.${item.path.split('.').pop().toLowerCase()}` : item.ext;
    saveShortcuts();
  };
  launch.addEventListener('click', async () => {
    sync();
    launch.classList.add('running');
    const r = await postJson('/api/shortcuts/run', { path: item.path });
    launch.classList.remove('running');
    if (!r.ok) alert(`Could not run shortcut:\n\n${r.stderr || JSON.stringify(r)}`);
  });
  row.querySelector('.shortcut-remove').addEventListener('click', () => {
    appShortcuts = appShortcuts.filter(x => x.id !== item.id);
    saveShortcuts();
    renderShortcuts();
  });
  return row;
}

function renderShortcuts() {
  const saved = $('shortcut-saved');
  if (!saved) return;
  saved.innerHTML = '';
  if (!appShortcuts.length) {
    saved.innerHTML = '<div class="no-data">🖱 No saved shortcuts yet.</div>';
  } else {
    saved.appendChild(el('div', { class: 'launcher-section-label' }, '🖱 saved'));
    appShortcuts.forEach(item => saved.appendChild(shortcutRow(item, 'saved')));
  }
}

function addShortcut(item) {
  if (!item?.path) return;
  if (appShortcuts.some(saved => saved.path === item.path)) {
    alert('That shortcut is already saved.');
    return;
  }
  appShortcuts.push({
    id: shortcutUid(),
    name: item.name || item.path.split('/').pop()?.replace(/\.[^.]+$/, '') || 'Shortcut',
    path: item.path,
    ext: item.ext || (item.path.includes('.') ? `.${item.path.split('.').pop().toLowerCase()}` : ''),
  });
  saveShortcuts();
  renderShortcuts();
}

function setupShortcutBrowse() {
  $('shortcut-browse')?.addEventListener('click', async () => {
    const r = await postJson('/api/shortcuts/scan', { dir: '/projects' });
    if (!r.ok) {
      alert(r.detail || 'Could not scan shortcut folder.');
      return;
    }
    const options = (r.shortcuts || []).filter(found => !appShortcuts.some(saved => saved.path === found.path));
    if (!options.length) {
      alert('No unsaved supported shortcut files found in /projects.');
      return;
    }
    const body = `<div class="shortcut-pick-list">${options.map((item, i) => `
      <button type="button" class="shortcut-pick" data-index="${i}">
        <span>${shortcutIcon(item)}</span>
        <strong>${escapeHtml(item.name)}</strong>
        <small>${escapeHtml(item.ext || '')}</small>
      </button>
    `).join('')}</div>`;
    $('modal')?.classList.add('shortcut-modal');
    $('modal-title').textContent = 'Add app shortcut';
    $('modal-body').innerHTML = body;
    $('modal-confirm').style.display = 'none';
    $('modal-bg').classList.add('show');
    $('modal-body').querySelectorAll('.shortcut-pick').forEach(btn => {
      btn.addEventListener('click', () => {
        const item = options[Number(btn.dataset.index)];
        addShortcut(item);
        $('modal-bg')?.classList.remove('show');
        $('modal')?.classList.remove('shortcut-modal');
        $('modal-confirm').style.display = '';
      });
    });
  });
}

function setupShortcuts() {
  setupShortcutBrowse();
  $('shortcut-add')?.addEventListener('click', () => {
    const path = prompt('Shortcut file path', '/Users/kylefleming/projects/');
    if (!path) return;
    const name = prompt('Shortcut name', path.split('/').pop()?.replace(/\.[^.]+$/, '') || 'Shortcut') || 'Shortcut';
    const ext = path.includes('.') ? `.${path.split('.').pop().toLowerCase()}` : '';
    addShortcut({ name, path, ext });
  });
  $('shortcut-clear')?.addEventListener('click', () => {
    confirmAction('Clear shortcuts?', 'Remove all saved App Shortcuts from this browser?', () => {
      appShortcuts = [];
      saveShortcuts();
      renderShortcuts();
    });
  });
  renderShortcuts();
}

setupShortcuts();

// ---- Dev Launcher ----
const LAUNCHER_STORE = 'sysdash_dev_launcher_v1';
const LAUNCHER_BASE_STORE = 'sysdash_dev_launcher_base_v1';
const LAUNCHER_EMOJIS = ['⚡', '🚀', '🛠', '🌐', '📦', '🐍', '🦀', '🐹', '⚙️'];
let launcherProjects = JSON.parse(localStorage.getItem(LAUNCHER_STORE) || '[]');
let launcherBasePath = localStorage.getItem(LAUNCHER_BASE_STORE) || '';
let launcherScan = [];

function migrateLauncherProjects() {
  let changed = false;
  launcherProjects = launcherProjects.map(project => {
    const haystack = [project.name, project.path, project.cwd, project.cmd].filter(Boolean).join(' ').toLowerCase();
    if (!haystack.includes('trading-pipeline')) return project;
    changed = true;
    return {
      ...project,
      name: 'sysdash',
      cmd: 'bash run.sh',
      type: 'Shell',
      emoji: '🛠',
      path: '.',
      cwd: '/Users/kylefleming/sysdash',
      hasRequirements: false,
      hasVenv: false,
    };
  });
  if (changed) saveLauncher();
}

function saveLauncher() {
  localStorage.setItem(LAUNCHER_STORE, JSON.stringify(launcherProjects));
}

function launcherUid() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

function launcherJoinPath(base, rel) {
  const cleanBase = (base || '').trim().replace(/\/+$/, '');
  let cleanRel = (rel || '').trim().replace(/^\/+|\/+$/g, '');
  const baseName = cleanBase.split('/').filter(Boolean).pop();
  if (baseName && cleanRel === baseName) cleanRel = '.';
  else if (baseName && cleanRel.startsWith(`${baseName}/`)) cleanRel = cleanRel.slice(baseName.length + 1);
  if (!cleanBase) return cleanRel;
  if (!cleanRel || cleanRel === '.') return cleanBase;
  return `${cleanBase}/${cleanRel}`;
}

function launcherShellQuotePath(path) {
  const value = (path || '').trim();
  if (!value) return "''";
  if (value === '~') return '$HOME';
  if (value.startsWith('~/')) {
    const rest = value.slice(2).replace(/["\\$`]/g, '\\$&');
    return `"$HOME/${rest}"`;
  }
  return `'${value.replace(/'/g, "'\\''")}'`;
}

function launcherProjectDir(project) {
  const input = $('launcher-base');
  if (input) launcherBasePath = input.value.trim();
  if (project.cwd) return project.cwd;
  return launcherJoinPath(launcherBasePath, project.path || '');
}

function launcherRuntimeCmd(project) {
  const cmd = (project.cmd || '').trim();
  const plainPython = cmd.match(/^python3?\s+([^\s;&|]+\.py)(.*)$/);
  if (!plainPython || /activate|\/bin\/python|python -m pip/.test(cmd)) return cmd;

  const script = plainPython[1];
  const args = plainPython[2] || '';
  const installDeps = project.hasRequirements ? '; python -m pip install -q -r requirements.txt' : '';
  return `if [ -d .venv ]; then . .venv/bin/activate; elif [ -d venv ]; then . venv/bin/activate; else python3 -m venv .venv && . .venv/bin/activate; fi${installDeps}; python ${script}${args}`;
}

function launcherPreviewCommand(project) {
  const cmd = launcherRuntimeCmd(project);
  const cwd = launcherProjectDir(project);
  if (!cmd || !cwd) return cmd;
  return `cd ${launcherShellQuotePath(cwd)} && ${cmd}`;
}

function launcherMeta(project) {
  const parts = [project.type || 'Project'];
  if (project.path) parts.push(project.path === '.' ? 'folder root' : project.path);
  const dir = launcherProjectDir(project);
  if (dir) parts.push(`cwd: ${dir}`);
  return parts.join(' · ');
}

function detectLauncherProjects(files) {
  const found = {};
  const ensure = (key, projectName, scanRoot) => {
    if (!found[key]) {
      found[key] = { id: launcherUid(), name: projectName, cmd: '', type: 'Project', emoji: '🚀', path: key, scanRoot, priority: 0 };
    }
    return found[key];
  };
  for (const file of files) {
    const rel = file.webkitRelativePath || file.name;
    const parts = rel.split('/').filter(Boolean);
    if (parts.length < 2 || parts.length > 4) continue;
    const name = parts[parts.length - 1];
    const dirParts = parts.slice(0, -1);
    const projectParts = dirParts.slice(1);
    const key = projectParts.join('/') || '.';
    const projectName = projectParts[projectParts.length - 1] || parts[0];

    if (name === 'pyvenv.cfg' && ['.venv', 'venv'].includes(dirParts[dirParts.length - 1])) {
      const envProjectParts = dirParts.slice(1, -1);
      const envKey = envProjectParts.join('/') || '.';
      const envProjectName = envProjectParts[envProjectParts.length - 1] || parts[0];
      ensure(envKey, envProjectName, parts[0]).hasVenv = true;
      continue;
    }

    const set = (priority, type, emoji, cmd, marker) => {
      if (!found[key] || found[key].priority < priority) {
        const prior = found[key] || {};
        found[key] = { ...prior, id: prior.id || launcherUid(), name: projectName, cmd, type, emoji, path: key, scanRoot: parts[0], priority, marker, file };
      }
    };

    if (name === 'package.json') set(10, 'Node.js', '⚡', 'npm run dev', 'package');
    else if (name === 'app.py' || name === 'main.py') set(8, 'Python', '🐍', `python3 ${name}`, 'python');
    else if (name === 'requirements.txt') {
      const project = ensure(key, projectName, parts[0]);
      project.hasRequirements = true;
      set(5, 'Python', '🐍', 'python3 app.py', 'python');
      found[key].hasRequirements = true;
    }
    else if (name === 'Cargo.toml') set(9, 'Rust', '🦀', 'cargo run', 'rust');
    else if (name === 'go.mod') set(9, 'Go', '🐹', 'go run .', 'go');
    else if (name === 'Makefile') set(7, 'Makefile', '⚙️', 'make', 'make');
    else if (name.endsWith('.sh')) set(6, 'Shell', '🛠', `bash ${name}`, 'shell');
  }
  return Object.values(found).filter(project => project.cmd);
}

async function processLauncherFiles(files) {
  const detected = detectLauncherProjects(files);
  launcherScan = await Promise.all(detected.map(async p => {
    if (p.marker === 'package' && p.file) {
      try {
        const pkg = JSON.parse(await p.file.text());
        const scripts = pkg.scripts || {};
        p.name = pkg.name || p.name;
        if (scripts.dev) p.cmd = 'npm run dev';
        else if (scripts.start) p.cmd = 'npm start';
        else if (scripts.serve) p.cmd = 'npm run serve';
        else p.cmd = 'npm start';
      } catch {
        p.cmd = 'npm start';
      }
    }
    return p;
  }));
  if (!launcherBasePath && launcherScan.length && launcherScan[0].scanRoot) {
    try {
      const inferred = await postJson('/api/launcher/infer-base', { root: launcherScan[0].scanRoot });
      if (inferred.ok && inferred.base) {
        launcherBasePath = inferred.base;
        localStorage.setItem(LAUNCHER_BASE_STORE, launcherBasePath);
        if ($('launcher-base')) $('launcher-base').value = launcherBasePath;
      }
    } catch {}
  }
  renderLauncher();
}

function launcherRow(project, mode) {
  const row = el('div', { class: 'launcher-row' });
  row.innerHTML = `
    <button class="launcher-launch" title="Launch in Terminal">
      <span class="launcher-emoji">${project.emoji || '⚡'}</span>
      <span class="launcher-name">${escapeHtml(project.name)}</span>
    </button>
    <div class="launcher-main">
      <input class="launcher-cmd" value="${escapeHtml(project.cmd || '')}" />
      <div class="launcher-meta">${escapeHtml(launcherMeta(project))}</div>
      <div class="launcher-health">checking...</div>
    </div>
    <div class="launcher-row-actions"></div>
  `;
  const cmdInput = row.querySelector('.launcher-cmd');
  const health = row.querySelector('.launcher-health');
  cmdInput.addEventListener('change', () => { project.cmd = cmdInput.value.trim(); saveLauncher(); });
  const launchTarget = row.querySelector('.launcher-launch');
  const actions = row.querySelector('.launcher-row-actions');

  if (mode === 'scan') {
    launchTarget.disabled = true;
    const add = el('button', { class: 'btn-go' }, '➕ add');
    add.addEventListener('click', () => {
      project.cmd = cmdInput.value.trim();
      launcherProjects.push({ id: launcherUid(), name: project.name, cmd: project.cmd, type: project.type, emoji: project.emoji, path: project.path, scanRoot: project.scanRoot, hasRequirements: !!project.hasRequirements, hasVenv: !!project.hasVenv });
      saveLauncher();
      launcherScan = launcherScan.filter(p => p !== project);
      renderLauncher();
    });
    actions.appendChild(add);
  } else {
    refreshLauncherHealth(project, health);
    launchTarget.addEventListener('click', () => {
      project.cmd = cmdInput.value.trim();
      saveLauncher();
      const cwd = launcherProjectDir(project);
      const command = launcherPreviewCommand(project);
      confirmAction('Launch project?', `Open Terminal and run <b>${escapeHtml(project.name)}</b>?<br><br><code>${escapeHtml(command)}</code>`, async () => {
        launchTarget.classList.add('running');
        const r = await postJson('/api/launcher/run', { name: project.name, cmd: launcherRuntimeCmd(project), cwd });
        launchTarget.classList.remove('running');
        if (!r.ok) alert(`Could not launch:\n\n${r.stderr || JSON.stringify(r)}`);
      });
    });
    const del = el('button', { class: 'btn-x' }, '🗑 remove');
    del.addEventListener('click', () => {
      launcherProjects = launcherProjects.filter(p => p.id !== project.id);
      saveLauncher();
      renderLauncher();
    });
    actions.appendChild(del);
  }
  return row;
}

async function refreshLauncherHealth(project, node) {
  if (!node) return;
  const cwd = launcherProjectDir(project);
  try {
    const r = await postJson('/api/launcher/health', { name: project.name, cmd: project.cmd, cwd });
    const cls = r.status === 'running' || r.status === 'ready' ? 'ok' : r.status === 'warn' ? 'warn' : 'bad';
    const checks = (r.checks || []).map(check => (
      `<span class="doctor-check ${check.ok ? 'ok' : check.fixable ? 'warn' : 'bad'}">${check.ok ? '✓' : check.fixable ? '!' : '×'} ${escapeHtml(check.label)}</span>`
    )).join('');
    node.innerHTML = `<div><span class="dot ${cls}"></span>${escapeHtml(r.detail || r.status || 'unknown')}</div>${checks ? `<div class="doctor-checks">${checks}</div>` : ''}`;
    if (r.fixable) {
      const fix = el('button', { class: 'btn-go launcher-fix' }, '🛠 fix');
      fix.addEventListener('click', () => {
        confirmAction('Fix launcher?', `Set up dependencies for <b>${escapeHtml(project.name)}</b>?`, async () => {
          fix.disabled = true;
          fix.textContent = '🛠 fixing';
          const result = await postJson('/api/launcher/fix', { name: project.name, cmd: project.cmd, cwd });
          fix.disabled = false;
          fix.textContent = '🛠 fix';
          alert(`${result.title || 'Launcher fix'}\n\n${result.detail || ''}`);
          refreshLauncherHealth(project, node);
        });
      });
      node.appendChild(fix);
    }
  } catch {
    node.innerHTML = '<span class="dot dim"></span>health check unavailable';
  }
}

function renderLauncher() {
  const results = $('launcher-results');
  const saved = $('launcher-saved');
  if (!results || !saved) return;

  results.innerHTML = '';
  if (launcherScan.length) {
    results.appendChild(el('div', { class: 'launcher-section-label' }, `🔎 ${launcherScan.length} found`));
    launcherScan.forEach(p => results.appendChild(launcherRow(p, 'scan')));
  }

  saved.innerHTML = '';
  if (!launcherProjects.length) {
    saved.innerHTML = '<div class="no-data">🚀 No saved launchers yet.</div>';
  } else {
    saved.appendChild(el('div', { class: 'launcher-section-label' }, '🚀 saved'));
    launcherProjects.forEach(p => saved.appendChild(launcherRow(p, 'saved')));
  }
}

if ($('launcher-drop')) {
  migrateLauncherProjects();
  const launcherBaseInput = $('launcher-base');
  if (launcherBaseInput) {
    launcherBaseInput.value = launcherBasePath;
    launcherBaseInput.addEventListener('change', () => {
      launcherBasePath = launcherBaseInput.value.trim();
      localStorage.setItem(LAUNCHER_BASE_STORE, launcherBasePath);
      renderLauncher();
    });
  }
  $('launcher-drop').addEventListener('click', () => $('launcher-folder').click());
  $('launcher-folder').addEventListener('change', ev => processLauncherFiles(Array.from(ev.target.files || [])));
  $('launcher-add').addEventListener('click', () => {
    launcherProjects.push({ id: launcherUid(), name: 'New Project', cmd: 'npm run dev', type: 'Manual', emoji: '🚀', path: '' });
    saveLauncher();
    renderLauncher();
  });
  $('launcher-clear').addEventListener('click', () => {
    confirmAction('Clear launchers?', 'Remove all saved Dev Launcher projects from this browser?', () => {
      launcherProjects = [];
      saveLauncher();
      renderLauncher();
    });
  });
  renderLauncher();
}

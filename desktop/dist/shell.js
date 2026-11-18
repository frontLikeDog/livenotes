'use strict';

/* ================================================================ base */
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const API = (window.__LN_ENGINE__ || '').replace(/\/+$/, '');
const ENTRY = window.__LN_ENTRY__ || '';
let ENGINE_ORIGIN = '';
try { ENGINE_ORIGIN = API ? new URL(API).origin : ''; } catch (e) { ENGINE_ORIGIN = ''; }
let ENTRY_SESSION = '';
try { ENTRY_SESSION = new URL(ENTRY || 'http://x/').searchParams.get('session') || ''; } catch (e) {}
const TAURI = window.__TAURI__;

const S = {
  view: 'listen',
  stage: 'signal',
  online: false,
  health: null,
  rooms: [],
  live: [],
  /* 采集端心跳里的流量快照：{roomId: {platform_mbps, audio_kbps, source, ...}}。
     /api/traffic 单独下发，不塞进 /api/rooms，房间元数据和实时遥测分开。 */
  traffic: {},
  sessions: [],
  selectedId: null,
  selectedSid: '',
  lines: [],
  speakers: [],
  speakersSid: '',
  partial: null,
  sessInfo: null,
  annotTab: 'all',
  annotAuto: true,
  annotSkipped: new Set(),
  /* 标注工作区的面板布局：房间列表 / 直播画面 / 字幕 / 本场声音 */
  panes: { side: true, insp: true, live: true, tx: true },
  annotLiveH: 0.52,
  annotMax: '',
  spkPop: null,
  filter: 'all',
  addOpen: false,
  busy: false,
  pageOpen: false,
  pageRoom: null,
  stageLabel: null,
  pageHidden: false,
  overlayDepth: 0,
  frameUrl: '',
  frameEmbedUrl: '',
  frameRawUrl: '',
  frameTitle: '',
  frameMeta: '',
  frameHead: false,
  frameRaw: false,
  frameBackView: 'listen',
  selectedSession: ENTRY_SESSION,
  paletteOpen: false,
  paletteIndex: 0,
  paletteActions: [],
  paletteSearch: { q: '', rows: [], seq: 0, timer: 0 },
  modalOpen: false,
  closeAction: 'tray',
  txBusy: false,
  lastToastAt: 0,
  trashCount: 0,
  batchMode: false,
  batchSel: new Set(),
  workbenchReady: false,
  workbenchSid: '',
  frameLoads: 0,
  frameKind: '',
  /* 性能档位：/api/perf 的最近一次结果，界面与轮询按它降频。 */
  perf: null,
  /* 原生工作台：一场的字幕、说话人、时间轴与 AI 总结。 */
  wb: {
    sid: '', rows: [], speakers: [], duration: 0, total: 0,
    spk: 'all', high: false, summaryScope: 'content',
    summaries: {}, coach: null, busy: false, seq: 0,
    audioReady: false, autoplay: false, seekT: null, currentT: 0,
    /* 标题候选与生成失败原因：面板里要能分清「AI 起的名」「本地旧稿」和「这次没生成」。 */
    titleCand: null, titleBusy: false, summaryError: null,
  },
  /* 原生设置：prefs 走引擎，主题走 localStorage。 */
  set: {
    pane: 'appearance', prefs: null, cfg: null, profiles: [], current: '',
    lan: null, search: '', loaded: false, busy: false,
  },
  /* 记录卡：当前盯住的这一路，以及结束后的三步收尾。 */
  hudSid: '',
  hudRoomId: '',
  hudClock: { elapsed: 0, at: 0, paused: false, sid: '' },
  /* 实时高光卡：同一时刻只露一条；「不用」后两分钟内不再弹。 */
  hudHi: { saved: new Set(), dismissUntil: 0, current: '' },
  finish: null,
  finishTimer: null,
  /* 通知：应用内中心 + 系统 toast。booted 之前只填充中心，不重播。 */
  notif: {
    items: [], unread: 0, latestId: 0, settings: null,
    booted: false, busy: false, open: false, sysDenied: false,
    lastPresent: null, lastOs: null,
  },
};

/* ================================================================ utils */
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));
const pad = (n) => String(n).padStart(2, '0');
const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
const fmtDur = (sec) => {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? h + ':' + pad(m) + ':' + pad(s) : pad(m) + ':' + pad(s);
};
const fmtClock = (iso) => {
  if (!iso) return '';
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!m) return String(iso).slice(0, 16);
  return m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5];
};
const hostOf = (u) => { try { return new URL(u).host.replace(/^www\./, ''); } catch (e) { return ''; } };
const shortSid = (sid) => String(sid || '').replace(/^LIVE_/, '').replace(/_/g, ' ');

/* 流量：字节与「折合每小时」。面板里按速率显示，读者关心的是会烧多少。 */
const fmtBytes = (n) => {
  n = Math.max(0, Number(n) || 0);
  if (n >= 1e9) return (n / 1e9).toFixed(2) + ' GB';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MB';
  if (n >= 1e3) return (n / 1e3).toFixed(0) + ' KB';
  return Math.round(n) + ' B';
};
const fmtMBPerHour = (mb) => fmtBytes((Number(mb) || 0) * 1e6) + '/h';
const fmtMbps = (mbps) => {
  mbps = Math.max(0, Number(mbps) || 0);
  return mbps >= 1 ? mbps.toFixed(2) + ' Mbps' : Math.round(mbps * 1000) + ' kbps';
};
const trafficSourceLabel = (src) => ({
  cdp: '实测 · 浏览器 Network',
  proxy: '实测 · 计数代理',
  estimate: '估算 · 码率/分辨率',
  mixed: '部分实测',
  unavailable: '不可用',
}[String(src || '')] || '—');
function trafficFor(room) {
  const tr = room && S.traffic ? S.traffic[room.id] : null;
  return tr && typeof tr === 'object' ? tr : null;
}
function liveTrafficTotals() {
  let n = 0, down = 0, up = 0;
  for (const r of liveRooms()) {
    const tr = trafficFor(r);
    if (!tr) continue;
    n += 1;
    down += Number(tr.platform_mb_per_hour) || 0;
    up += Number(tr.audio_mb_per_hour) || 0;
  }
  return { n, down, up };
}

/* 位置标签不是人名：与 app/voiceprint.py 的 GENERIC_NAMES 对齐。
   改名前如果只是「嘉宾 / 运营」，就只算角色标注，不算认出这个人。 */
const GENERIC_RE = /^(主播|嘉宾|运营|说话人|未知|路人|观众|网友|连麦|有人|未识别|用户|SPK[_-]?\d+)$/i;
const isGenericName = (name) => {
  const n = String(name == null ? '' : name).trim();
  return !n || GENERIC_RE.test(n) || /^(嘉宾|说话人|用户|观众)\s*\d+$/.test(n);
};
const SPK_COLORS = ['#E8C47D', '#9CC8B0', '#B9A0E8', '#8FB4D9', '#E89088', '#D7C08A', '#A9C7E8'];
const SPK_COLORS_LIGHT = ['#8A5A0E', '#2F7D5F', '#5B4A8A', '#3F6F9E', '#A8453A', '#7A6220', '#4E7288'];
function spkColor(id) {
  let h = 0;
  const s = String(id || '');
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  const pal = resolvedTheme() === 'light' ? SPK_COLORS_LIGHT : SPK_COLORS;
  return pal[Math.abs(h) % pal.length];
}

/* 直播 status 和场后 /api/speakers 的字段名不同，统一成一种形状再画。 */
function normSpeaker(sp) {
  const id = String(sp.speaker_id || sp.id || '');
  const label = String(sp.label || sp.speaker_label || id || '说话人');
  const manual = sp.manual != null ? !!sp.manual : !!sp.verified;
  return {
    id, label,
    seconds: Math.max(0, Number(sp.seconds) || 0),
    count: Number(sp.count != null ? sp.count : (sp.n || 0)) || 0,
    manual,
    personState: String(sp.person_state || ''),
    personName: String(sp.person_name || ''),
    score: sp.person_score != null && sp.person_score !== '' ? Number(sp.person_score) : null,
    verified: !!sp.verified,
  };
}
function spkKind(sp) {
  if (!sp) return 'unknown';
  if (sp.personState === 'rejected') return 'rejected';
  if (sp.manual) return isGenericName(sp.label) ? 'role' : 'confirmed';
  if (sp.personState === 'confirmed') return 'confirmed';
  if (sp.personState === 'auto') return 'auto';
  if (sp.personState === 'cand' || sp.personState === 'candidate') return 'candidate';
  if (sp.personName) return 'candidate';
  if (sp.verified && !isGenericName(sp.label)) return 'confirmed';
  return 'unknown';
}
const isClaimed = (kind) => kind !== 'unknown' && kind !== 'rejected';
function annotSpeakers() {
  return (S.speakers || []).map(normSpeaker).filter((s) => s.id)
    .sort((a, b) => b.seconds - a.seconds || String(a.id).localeCompare(String(b.id)));
}
function spkMap() {
  const m = new Map();
  annotSpeakers().forEach((s) => m.set(s.id, s));
  return m;
}
function unclaimedCount(sess) {
  if (!sess || !Array.isArray(sess.speakers)) return 0;
  return sess.speakers.map(normSpeaker).filter((s) => {
    const k = spkKind(s);
    return (k === 'unknown' || k === 'rejected') && s.seconds >= 5;
  }).length;
}

function invoke(cmd, args) {
  if (!TAURI || !TAURI.core || !TAURI.core.invoke) {
    return Promise.reject(new Error('桌面 IPC 不可用'));
  }
  return TAURI.core.invoke(cmd, args);
}

async function api(path, opts) {
  const res = await fetch(API + path, Object.assign({ cache: 'no-store' }, opts || {}));
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}
async function post(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

function toast(msg, action) {
  const el = $('#toast');
  el.textContent = '';
  const text = document.createElement('span');
  text.textContent = msg;
  el.appendChild(text);
  if (action && action.label && typeof action.run === 'function') {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'toast-act';
    btn.textContent = action.label;
    btn.onclick = () => {
      el.hidden = true;
      action.run();
    };
    el.appendChild(btn);
  }
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, action ? 5200 : 2800);
}

function copyText(text) {
  const value = String(text || '');
  if (!value) return;
  const fallback = () => {
    try {
      const ta = document.createElement('textarea');
      ta.value = value;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
      toast('已复制');
    } catch (e) { toast('复制失败'); }
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(value).then(() => toast('已复制')).catch(fallback);
  } else {
    fallback();
  }
}

/* ================================================================ 应用内右键菜单
   WebView2 的原生菜单在 Rust 侧已经关掉；这里接管场次/房间/输入框。
   菜单只在对象上出现，空白画布不弹。 */
const CTX = { items: [], index: -1 };

// 菜单图标用内联 SVG，不依赖系统字体里有没有 ⧉/⤓ 这类符号。
const CTX_ICONS = {
  open: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  play: '<svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
  rename: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13 5l4 4"/></svg>',
  copy: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="8" y="8" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>',
  export: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
  trash: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',
  page: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 9h16"/></svg>',
  link: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M10 14a4 4 0 0 0 5.7 0l2.3-2.3a4 4 0 0 0-5.7-5.7L11 7"/><path d="M14 10a4 4 0 0 0-5.7 0L6 12.3a4 4 0 0 0 5.7 5.7L13 17"/></svg>',
  retry: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M20 11a8 8 0 1 0-2 5"/><path d="M20 4v7h-7"/></svg>',
  stop: '<svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>',
  remove: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  record: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M9 21h6"/></svg>',
};
function ctxIcon(name) { return CTX_ICONS[name] || ''; }

function closeCtxMenu() {
  const el = $('#ctxMenu');
  if (!el || el.hidden) return;
  el.hidden = true;
  el.innerHTML = '';
  CTX.items = [];
  CTX.index = -1;
}

function paintCtxIndex() {
  $$('#ctxMenu .ctx-item').forEach((b) => {
    b.classList.toggle('on', Number(b.dataset.i) === CTX.index);
  });
  const cur = $('#ctxMenu .ctx-item.on');
  if (cur) cur.focus({ preventScroll: true });
}

function showCtxMenu(items, x, y) {
  const el = $('#ctxMenu');
  if (!el) return;
  CTX.items = (items || []).filter(Boolean);
  if (!CTX.items.length) return;
  el.innerHTML = CTX.items.map((it, i) => {
    if (it.sep) return '<div class="ctx-sep"></div>';
    const cls = 'ctx-item' + (it.danger ? ' danger' : '') +
      (it.disabled ? ' disabled' : '');
    return '<button class="' + cls + '" data-i="' + i + '" role="menuitem"' +
      (it.disabled ? ' disabled' : '') + '>' +
      '<span class="ctx-ico">' + ctxIcon(it.icon) + '</span>' +
      '<span class="ctx-label">' + esc(it.label) + '</span>' +
      (it.hint ? '<span class="ctx-hint">' + esc(it.hint) + '</span>' : '') +
      '</button>';
  }).join('');
  el.hidden = false;
  el.style.left = '0px';
  el.style.top = '0px';
  const r = el.getBoundingClientRect();
  const pad = 8;
  el.style.left = Math.max(pad, Math.min(x, window.innerWidth - r.width - pad)) + 'px';
  el.style.top = Math.max(pad, Math.min(y, window.innerHeight - r.height - pad)) + 'px';
  const first = CTX.items.findIndex((it) => !it.sep && !it.disabled);
  CTX.index = first;
  paintCtxIndex();
}

function runCtxItem(i) {
  const it = CTX.items[i];
  if (!it || it.sep || it.disabled) return;
  closeCtxMenu();
  try { if (it.run) it.run(); } catch (e) { toast(String(e && e.message || e)); }
}

function ctxKey(e) {
  const el = $('#ctxMenu');
  if (!el || el.hidden) return;
  const usable = CTX.items
    .map((it, i) => ({ it, i }))
    .filter((x) => !x.it.sep && !x.it.disabled)
    .map((x) => x.i);
  const at = usable.indexOf(CTX.index);
  if (e.key === 'Escape') {
    e.preventDefault(); e.stopPropagation(); closeCtxMenu(); return;
  }
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault(); e.stopPropagation();
    if (!usable.length) return;
    const next = e.key === 'ArrowDown'
      ? usable[(at + 1) % usable.length]
      : usable[(at - 1 + usable.length) % usable.length];
    CTX.index = next;
    paintCtxIndex();
    return;
  }
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault(); e.stopPropagation();
    runCtxItem(CTX.index);
  }
}

function initContextMenu() {
  const el = $('#ctxMenu');
  if (!el) return;
  el.addEventListener('click', (e) => {
    const b = e.target.closest('[data-i]');
    if (b) runCtxItem(Number(b.dataset.i));
  });
  document.addEventListener('keydown', ctxKey, true);
  document.addEventListener('mousedown', (e) => {
    if (!el.hidden && !el.contains(e.target)) closeCtxMenu();
  }, true);
  document.addEventListener('scroll', closeCtxMenu, true);
  window.addEventListener('blur', closeCtxMenu);
  window.addEventListener('resize', closeCtxMenu);
  // 兜底：即使 WebView2 设置没生效，也不让原生网页菜单露出来。
  // 输入框/可编辑区给一个最小编辑菜单，其余空白区域不弹。
  document.addEventListener('contextmenu', (e) => {
    const editable = e.target.closest('input, textarea, [contenteditable="true"]');
    if (editable) {
      e.preventDefault();
      showCtxMenu([
        { icon: '', label: '剪切', hint: 'Ctrl+X', run: () => { try { document.execCommand('cut'); } catch (err) {} } },
        { icon: 'copy', label: '复制', hint: 'Ctrl+C', run: () => { try { document.execCommand('copy'); } catch (err) {} } },
        { icon: '', label: '粘贴', hint: 'Ctrl+V', run: () => {
          if (navigator.clipboard && navigator.clipboard.readText) {
            navigator.clipboard.readText().then((t) => {
              try { document.execCommand('insertText', false, t); } catch (err) { toast('请用 Ctrl+V 粘贴'); }
            }).catch(() => toast('请用 Ctrl+V 粘贴'));
          } else { toast('请用 Ctrl+V 粘贴'); }
        } },
        { sep: true },
        { icon: '', label: '全选', hint: 'Ctrl+A', run: () => { try { document.execCommand('selectAll'); } catch (err) {} } },
      ], e.clientX, e.clientY);
      return;
    }
    const sel = window.getSelection();
    if (sel && String(sel).trim()) {
      e.preventDefault();
      showCtxMenu([{ icon: 'copy', label: '复制选中文字', hint: 'Ctrl+C', run: () => copyText(String(sel)) }],
        e.clientX, e.clientY);
      return;
    }
    e.preventDefault();
    closeCtxMenu();
  });
}

/* ================================================================ model */
function sessionFor(room) {
  if (!room || !room.session_id) return null;
  return S.live.find((s) => s.id === room.session_id) || null;
}
function stateInfo(room) {
  if (!room) return { k: 'stopped', label: '未选择' };
  const live = !!sessionFor(room) || room.state === 'live';
  if (room.state === 'failed' || (room.error && !live)) return { k: 'failed', label: '异常' };
  if (room.state === 'stopped') return { k: 'stopped', label: '已停止' };
  if (live) return { k: 'live', label: '在听' };
  return { k: 'pending', label: '启动中' };
}
function roomCh(room) {
  if (!room) return 'CH--';
  const seq = Number(room.seq) || (S.rooms.indexOf(room) + 1);
  return 'CH' + pad(seq);
}
function roomName(room) {
  if (!room) return '选择一个直播间';
  return room.name || hostOf(room.url) || '未命名直播间';
}
function captureLabel(room) {
  const v = room && room.capture_engine;
  return { direct: '直取', shared: '共享浏览器', per_room: '独立浏览器' }[v] || '—';
}
function liveRooms() {
  return S.rooms.filter((r) => stateInfo(r).k === 'live');
}
function selectedRoom() {
  return S.rooms.find((r) => r.id === S.selectedId) || null;
}
/* 记录卡盯住的那一路。和左侧选中项解耦：HUD 里不需要用户先在列表里点。 */
function hudRoom() {
  if (S.hudRoomId) {
    const r = S.rooms.find((x) => x.id === S.hudRoomId);
    if (r) return r;
  }
  if (S.hudSid) {
    const r = S.rooms.find((x) => x.session_id === S.hudSid);
    if (r) return r;
  }
  const live = liveRooms();
  if (live.length === 1) return live[0];
  return live.length ? live[live.length - 1] : null;
}
function activeRoom() {
  return S.view === 'hud' ? hudRoom() : selectedRoom();
}
function ensureSelection() {
  if (S.selectedId && S.rooms.some((r) => r.id === S.selectedId)) return;
  const first = S.rooms.find((r) => stateInfo(r).k === 'live') || S.rooms[0] || null;
  S.selectedId = first ? first.id : null;
  S.lines = [];
  S.selectedSid = first ? (first.session_id || '') : '';
  if (S.pageOpen && S.pageRoom !== S.selectedId) closeStageView();
}

/* ================================================================ data */
async function refreshRooms() {
  if (!API) { S.online = false; renderAll(); return; }
  try {
    const [roomsRes, liveRes, health, trafficRes] = await Promise.all([
      api('/api/rooms'),
      api('/api/live/list').catch(() => ({ sessions: [] })),
      api('/api/health').catch(() => null),
      api('/api/traffic').catch(() => ({ rooms: [] })),
    ]);
    S.rooms = (roomsRes.rooms || []).slice().sort((a, b) => {
      const rank = (r) => ({ live: 0, pending: 1, failed: 2, stopped: 3 }[stateInfo(r).k] ?? 3);
      return rank(a) - rank(b) || (Number(a.seq) || 0) - (Number(b.seq) || 0);
    });
    S.live = liveRes.sessions || [];
    S.traffic = {};
    for (const row of (trafficRes.rooms || [])) {
      if (row && row.id) S.traffic[row.id] = row;
    }
    S.health = health;
    S.online = true;
  } catch (e) {
    S.online = false;
    S.health = null;
    S.traffic = {};
  }
  ensureSelection();
  const room = selectedRoom();
  const sid = room ? (room.session_id || '') : '';
  if (sid !== S.selectedSid) { S.selectedSid = sid; S.lines = []; }
  if (S.view === 'hud') {
    const hr = hudRoom();
    if (hr) {
      S.hudRoomId = hr.id;
      if (hr.session_id) S.hudSid = hr.session_id;
    }
  }
  renderAll();
}

async function loadSessions() {
  const had = S.selectedSession;
  try {
    const d = await api('/api/sessions');
    S.sessions = d.sessions || [];
  } catch (e) {
    S.sessions = [];
  }
  refreshTrashCount();
  // 默认打开一场“有内容且能回放”的场次，而不是列表最上面的空会话。
  // 提前在启动时选中，切到工作台就只加载一次 iframe，不会先白一下再重载。
  const alive = !!S.selectedSession && S.sessions.some((s) => s.id === S.selectedSession);
  if (!alive) {
    const best = S.sessions.find((s) => s.audio_state === 'ready' && Number(s.n_utterances) > 0)
      || S.sessions.find((s) => Number(s.n_utterances) > 0)
      || S.sessions[0];
    S.selectedSession = best ? best.id : '';
  }
  if (S.view !== 'workbench') return;
  if (S.selectedSession && S.selectedSession !== S.workbenchSid) {
    setWorkbenchFrame(S.selectedSession);
  }
  renderSidebar();
}

function setWorkbenchFrame(sid, autoplay, seekT) {
  // 工作台已经原生化；保留这个入口给旧调用方（重命名、消息跳转）。
  openWorkbench(sid || S.selectedSession || '', { autoplay: !!autoplay, t: seekT });
}

function postToFrame(msg) {
  const f = $('#frame');
  if (!f || !f.contentWindow) return;
  try { f.contentWindow.postMessage(msg, ENGINE_ORIGIN || '*'); } catch (e) {}
}

async function refreshTrashCount() {
  if (!API) return;
  try {
    const d = await api('/api/library');
    S.trashCount = Number((d.stats || {}).trash) || 0;
    if (S.view === 'workbench') renderSidebar();
  } catch (e) {}
}

let txBusy = false;
async function pollTranscript() {
  const room = activeRoom();
  if (!['listen', 'hud'].includes(S.view) || !room || !room.session_id) return;
  // 标注模式里页面是打开的，但字幕必须继续轮询；只有普通页面模式才停。
  if (S.pageOpen && S.stage !== 'annotate') return;
  if (txBusy) return;
  txBusy = true;
  try {
    let rows = [];
    if (stateInfo(room).k === 'live') {
      const d = await api('/api/live/status?id=' + encodeURIComponent(room.session_id));
      rows = Array.isArray(d.utterances) ? d.utterances
        : (d.utterances && Array.isArray(d.utterances.rows) ? d.utterances.rows : []);
      S.speakers = Array.isArray(d.speakers) ? d.speakers : [];
      S.speakersSid = room.session_id;
      S.partial = d.partial || null;
      S.sessInfo = d;
    } else {
      // 已停止 / 异常的房间没有实时会话，回放最后一次会话的字幕，
      // 总比停在一句「还在等第一句」有用。
      const d = await api('/api/utterances?session_id=' + encodeURIComponent(room.session_id) + '&limit=120');
      rows = (Array.isArray(d.rows) ? d.rows : []).slice().sort((a, b) => {
        const ta = a.t_start != null ? a.t_start : (a.t || 0);
        const tb = b.t_start != null ? b.t_start : (b.t || 0);
        return ta - tb;
      });
      S.partial = null;
      S.sessInfo = null;
      if (S.speakersSid !== room.session_id) {
        try {
          const sp = await api('/api/speakers?session_id=' + encodeURIComponent(room.session_id));
          S.speakers = Array.isArray(sp.speakers) ? sp.speakers : [];
          S.speakersSid = room.session_id;
        } catch (e) { S.speakers = []; }
      }
    }
    S.lines = rows.slice(-80);
    S.health = S.health || {};
    if (S.stage === 'annotate') renderAnnot();
    renderInspector();
    if (S.view === 'hud') renderHud();
  } catch (e) {
    /* 会话可能刚结束；下一次 rooms 轮询会修正 */
  } finally {
    txBusy = false;
  }
}

function perfNum(key, fallback) {
  const v = S.perf && S.perf.effective && S.perf.effective[key];
  const n = Number(v);
  return n > 0 ? n : fallback;
}
function perfLite() {
  return !!(S.perf && S.perf.effective && S.perf.effective.animations === false);
}
async function loadPerf() {
  try {
    const d = await api('/api/perf');
    S.perf = d;
    document.body.classList.toggle('perf-lite', !!perfLite());
  } catch (e) {
    /* 旧引擎没有 /api/perf：按流畅档继续。 */
  }
  return S.perf;
}
async function pollRoomsLoop() {
  await refreshRooms();
  setTimeout(pollRoomsLoop, S.online ? perfNum('live_poll_s', 2) * 1000 : 5000);
}
async function pollTranscriptLoop() {
  await pollTranscript();
  setTimeout(pollTranscriptLoop, 1500);
}
async function pollSessionsLoop() {
  if (S.view === 'workbench') await loadSessions();
  setTimeout(pollSessionsLoop, 15000);
}

/* ================================================================ views */
/* ================================================================ 主题 */
// 整窗明暗：light / dark / system，存在本机 localStorage。
// 设置 → 外观和顶栏的太阳 / 月亮共用同一份选择。
const THEME_KEY = 'ln.theme';
const THEME_MEDIA = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
let themeChoice = 'dark';
try {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved && ['light', 'dark', 'system'].includes(saved)) themeChoice = saved;
} catch (e) {}
function resolvedTheme() {
  if (themeChoice === 'system') return THEME_MEDIA && THEME_MEDIA.matches ? 'dark' : 'light';
  return themeChoice;
}
function applyTheme() {
  const t = resolvedTheme();
  document.documentElement.dataset.theme = t;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', t === 'dark' ? '#0A0C0F' : '#F1F2F3');
  const btn = $('#themeToggle');
  if (btn) {
    const title = t === 'dark' ? '切换到亮色模式' : '切换到暗色模式';
    btn.title = title;
    btn.setAttribute('aria-label', title);
  }
  $$('.theme-card').forEach((card) => {
    const on = card.dataset.themeChoice === themeChoice;
    card.classList.toggle('is-active', on);
    const radio = card.querySelector('input[type="radio"]');
    if (radio) radio.checked = on;
  });
}
function setTheme(choice, opts) {
  themeChoice = ['light', 'dark', 'system'].includes(choice) ? choice : 'dark';
  try { localStorage.setItem(THEME_KEY, themeChoice); } catch (e) {}
  applyTheme();
  // 内嵌的引擎页只认 paper / studio 两套覆盖；主题变化时重载当前页面。
  if (S.frameEmbedUrl && ['files', 'people', 'live', 'frame',
      'follows', 'library', 'weekly', 'delivery', 'note'].includes(S.view)) {
    const f = $('#frame');
    if (f) f.src = withTheme(S.frameEmbedUrl);
  }
  if (S.view === 'workbench' && S.wb.sid && S.wb.rows.length) renderWbAll();
  if (!opts || !opts.silent) markSaved();
}
function toggleTheme() {
  setTheme(resolvedTheme() === 'dark' ? 'light' : 'dark');
}
function withTheme(url) {
  // 引擎页只有 paper（暖纸亮面）与 studio（深色壳层）两套覆盖。
  const theme = resolvedTheme() === 'dark' ? 'studio' : 'paper';
  try {
    const u = new URL(url, location.href);
    u.searchParams.set('theme', theme);
    return u.toString();
  } catch (e) { return url; }
}
if (THEME_MEDIA) {
  const onScheme = () => { if (themeChoice === 'system') setTheme('system', { silent: true }); };
  if (THEME_MEDIA.addEventListener) THEME_MEDIA.addEventListener('change', onScheme);
  else if (THEME_MEDIA.addListener) THEME_MEDIA.addListener(onScheme);
}
applyTheme();

const VIEW_META = {
  hud: { title: '记录', sub: '录音与收尾', side: false, insp: false },
  listen: { title: '收听', sub: '全部直播间', side: true, insp: true },
  workbench: { title: '工作台', sub: '时间轴与总结', side: true, insp: true },
  follows: { title: '关注', sub: '开播提醒与自动记录', side: false, insp: false },
  library: { title: '笔记库', sub: '每一场的要点与高光', side: false, insp: false },
  weekly: { title: '周报', sub: '这一周的回看', side: false, insp: false },
  delivery: { title: '精修交付', sub: '报告与交付包', side: false, insp: false },
  note: { title: '笔记', sub: '要点、高光与原话', side: false, insp: false },
  files: { title: '文件中心', sub: '录音与回放', side: false, insp: false },
  people: { title: '人物档案', sub: '声纹与确认', side: false, insp: false },
  tools: { title: '内容助手', sub: '圆桌摘要与名片', side: false, insp: false },
  settings: { title: '设置', sub: '外观与连接', side: false, insp: false },
  live: { title: '实时字幕', sub: '正在监听', side: false, insp: false },
  frame: { title: '页面', sub: '', side: false, insp: false },
};

function switchView(view, opts) {
  opts = opts || {};
  // 多路时 HUD 不抢岗位：设计上 2 路以上留在收听多路视图，用户选一路再进记录卡。
  // 但拦截必须给下一步：toast 直接把人带到舞台上方那条 picker（renderMultiBar），
  // 否则用户只会看到一句「先在这里选一路」，而收听页没有任何入口。
  if (view === 'hud' && !opts.force && !S.finish && liveRooms().length > 1) {
    view = 'listen';
    toast('有 ' + liveRooms().length + ' 个直播间在记录，先在这里选一路。', {
      label: '选一路',
      run: () => pickMultiRoom(),
    });
  }
  if (S.view === 'listen' && view !== 'listen') closeStageView();
  if (S.paletteOpen) closePalette();
  S.view = view;
  S.stage = view === 'listen' ? S.stage : 'signal';
  applyTheme();
  document.body.classList.toggle('hud-mode', view === 'hud');

  const meta = VIEW_META[view] || VIEW_META.listen;
  const body = $('#body');
  // 设置是整页；收听尊重用户自己开合的面板；其它页面按视图默认。
  const wantSide = view !== 'settings' && (view === 'listen' ? S.panes.side : meta.side);
  const wantInsp = view !== 'settings' && (view === 'listen' ? S.panes.insp : meta.insp);
  body.classList.toggle('no-side', !wantSide);
  body.classList.toggle('no-insp', !wantInsp);
  body.classList.toggle('set-mode', view === 'settings');
  applyPaneButtons();
  $('#listenView').hidden = view !== 'listen';
  $('#hudView').hidden = view !== 'hud';
  $('#wbView').hidden = view !== 'workbench';
  $('#setView').hidden = view !== 'settings';
  $('#frameView').hidden = !['files', 'people', 'live', 'frame',
    'follows', 'library', 'weekly', 'delivery', 'note', 'tools'].includes(view);

  if (view === 'hud') {
    if (opts.room) S.hudRoomId = opts.room;
    if (opts.session) S.hudSid = opts.session;
    const r = hudRoom();
    if (r) {
      S.hudRoomId = r.id;
      if (r.session_id) S.hudSid = r.session_id;
    }
    renderHud();
  } else if (view === 'workbench') {
    const sid = opts.session || S.selectedSession || '';
    S.selectedSession = sid;
    openWorkbench(sid, { autoplay: opts.autoplay, t: opts.t });
    if (!S.sessions.length) loadSessions();
  } else if (view === 'settings') {
    openSettings();
  } else if (view === 'files') {
    const tab = ['trash', 'cleanup'].includes(opts.tab) ? opts.tab : 'live';
    setFrame(API + '/files?embed=1&tab=' + tab,
      API + '/files?tab=' + tab, '文件中心', '录音与回放', true);
    S.frameBackView = 'listen';
  } else if (view === 'follows') {
    setFrame(API + '/follows?embed=1', API + '/follows',
      '关注', '开播提醒与自动记录', false);
    S.frameBackView = 'listen';
  } else if (view === 'library') {
    const search = ['notes', 'lines', 'follows', 'ask'].includes(opts.search)
      ? opts.search : '';
    let extra = '';
    if (search) {
      extra = '&search=' + search +
        (opts.q ? '&q=' + encodeURIComponent(opts.q) : '');
    }
    setFrame(API + '/library?embed=1' + extra,
      API + '/library' + (extra ? '?' + extra.slice(1) : ''),
      '笔记库', '每一场的要点与高光', false);
    S.frameBackView = 'listen';
  } else if (view === 'weekly') {
    const period = /^\d{4}-W\d{1,2}$/.test(String(opts.period || ''))
      ? String(opts.period) : '';
    const q = 'embed=1' + (period ? '&period=' + encodeURIComponent(period) : '');
    setFrame(API + '/weekly?' + q,
      API + '/weekly' + (period ? '?period=' + encodeURIComponent(period) : ''),
      '周报', period || '这一周的回看', false);
    S.frameBackView = 'listen';
  } else if (view === 'delivery') {
    const pid = String(opts.id || '');
    const q = 'embed=1' + (pid ? '&id=' + encodeURIComponent(pid) : '');
    setFrame(API + '/delivery?' + q,
      API + '/delivery' + (pid ? '?id=' + encodeURIComponent(pid) : ''),
      '精修交付', '报告与交付包', false);
    S.frameBackView = 'listen';
  } else if (view === 'note') {
    const sid = opts.session || S.selectedSession || '';
    const t = opts.t != null && opts.t !== ''
      ? '&t=' + encodeURIComponent(opts.t) : '';
    const raw = '/note?session_id=' + encodeURIComponent(sid) + t;
    setFrame(API + raw + '&embed=1', API + raw,
      '笔记', sid ? shortSid(sid) : '', false);
    S.frameBackView = opts.back || 'library';
  } else if (view === 'people') {
    setFrame(API + '/people?embed=1', API + '/people', '人物档案', '声纹与确认', true);
    S.frameBackView = 'listen';
  } else if (view === 'tools') {
    setFrame(API + '/tools?embed=1', API + '/tools', '内容助手', '圆桌摘要与名片', false);
    S.frameBackView = 'library';
  } else if (view === 'live') {
    const sid = opts.session || S.selectedSession || '';
    const q = 'embed=1' + (sid ? '&session=' + encodeURIComponent(sid) : '');
    setFrame(API + '/live?' + q, API + '/live' + (sid ? '?session=' + encodeURIComponent(sid) : ''),
      '实时字幕', sid ? shortSid(sid) : '全部在听', true);
    S.frameBackView = opts.back || 'listen';
  } else if (view === 'frame') {
    setFrame(opts.url, opts.rawUrl || opts.url, opts.title || '页面', opts.meta || '', true);
    S.frameBackView = opts.back || 'listen';
  }
  // 旧页面是嵌进来的：用一圈画布边框包住；实时字幕本身就是深色全屏页。
  $('#frameView').classList.toggle('framed', ['files', 'people', 'frame'].includes(view));

  $('#crumb').innerHTML = '<b>' + esc(meta.title) + '</b><i>/</i><span>' + esc(meta.sub) + '</span>';
  document.title = 'LiveNotes · ' + meta.title;
  const label = $('#cmdkLabel');
  if (label) label.textContent = view === 'settings' ? '搜索设置' : '搜索或执行命令';
  renderRail();
  renderSidebar();
  renderStage();
  renderInspector();
  renderHud();
  renderStatus();
}

function setFrame(embedUrl, rawUrl, title, meta, head) {
  S.frameEmbedUrl = embedUrl;
  S.frameRawUrl = rawUrl;
  S.frameTitle = title;
  S.frameMeta = meta || '';
  S.frameHead = !!head;
  S.frameRaw = false;
  S.workbenchReady = false;
  S.frameKind = '';
  $('#frameTitle').textContent = title;
  $('#frameMeta').textContent = meta || '';
  $('#frameView').classList.toggle('no-head', !head);
  $('#frameRaw').textContent = '原工具栏';
  $('#frame').src = withTheme(embedUrl);
}

/* ================================================================ render */
function renderAll() {
  renderRail();
  renderSidebar();
  renderStage();
  renderInspector();
  renderHud();
  renderStatus();
}

function renderRail() {
  let active = (S.view === 'live' || S.view === 'frame') ? 'listen' : S.view;
  if (S.view === 'note') active = 'library';
  $$('.rail-item').forEach((b) => b.classList.toggle('on', b.dataset.view === active));
  const n = liveRooms().length;
  const badge = $('#railBadge');
  badge.hidden = !n;
  badge.textContent = String(n);
  const rec = $('#railRec');
  if (rec) rec.hidden = n === 0;
}

function renderSidebar() {
  const meta = VIEW_META[S.view] || VIEW_META.listen;
  $('#sideTitle').textContent = S.view === 'listen' ? '在听' : (S.view === 'workbench' ? '场次' : meta.title);
  $('#btnAdd').hidden = S.view !== 'listen';
  $('#sideFoot').hidden = false;
  $('#sideFoot').textContent = S.view === 'listen'
    ? '关掉窗口不影响录音 · 最多 5 路'
    : '点场次打开工作台 · 数据来自本地引擎';
  const tools = $('#sideTools');
  const list = $('#sideList');
  // 轮询会反复调 renderSidebar；内容没变就不要重建列表，滚动位置也保住。
  const setList = (html) => {
    if (list.dataset.render === html) return;
    const top = list.scrollTop;
    list.innerHTML = html;
    list.dataset.render = html;
    list.scrollTop = top;
  };
  const count = S.view === 'listen' ? S.rooms.length : S.sessions.length;
  $('#sideCount').textContent = String(count);

  if (S.view === 'listen') {
    const liveN = liveRooms().length;
    const badN = S.rooms.filter((r) => stateInfo(r).k === 'failed').length;
    const stoppedN = S.rooms.filter((r) => stateInfo(r).k === 'stopped').length;
    tools.innerHTML =
      '<button class="chip ' + (S.filter === 'live' ? 'on' : '') + '" data-filter="live">在听 <span class="n">' + liveN + '</span></button>' +
      '<button class="chip ' + (S.filter === 'all' ? 'on' : '') + '" data-filter="all">全部 <span class="n">' + S.rooms.length + '</span></button>' +
      (badN ? '<button class="chip ' + (S.filter === 'issue' ? 'on' : '') + '" data-filter="issue">异常 <span class="n">' + badN + '</span></button>' : '');
    $('#sideFoot').innerHTML = '关掉窗口不影响录音 · 最多 5 路' +
      (stoppedN ? ' · <button class="link danger" data-clear-stopped>清除已停止 ' + stoppedN + '</button>' : '');
    const rows = S.rooms.filter((r) => {
      const k = stateInfo(r).k;
      if (S.filter === 'live') return k === 'live';
      if (S.filter === 'issue') return k === 'failed';
      return true;
    });
    setList(rows.length ? rows.map(roomRowHtml).join('') : (
      '<div class="side-empty">' + (S.rooms.length
        ? '这个筛选下没有直播间。'
        : '还没有在听的直播间。<br>点右上角 ＋ 粘贴链接开始。') + '</div>'
    ));
    return;
  }

  if (S.view === 'workbench') {
    const ids = new Set(S.sessions.map((s) => s.id));
    for (const id of Array.from(S.batchSel)) if (!ids.has(id)) S.batchSel.delete(id);
    tools.innerHTML =
      '<button class="chip on">全部场次 <span class="n">' + S.sessions.length + '</span></button>' +
      '<button class="chip ' + (S.batchMode ? 'on' : '') + '" data-batch-toggle>批量</button>' +
      (S.batchMode ? '<button class="chip" data-batch-all>全选</button>' : '');
    $('#sideFoot').innerHTML = S.batchMode
      ? ('已选 <b>' + S.batchSel.size + '</b> 场 · ' +
         '<button class="link" data-batch-trash' + (S.batchSel.size ? '' : ' disabled') + '>移到回收站</button> · ' +
         '<button class="link" data-batch-export' + (S.batchSel.size ? '' : ' disabled') + '>导出</button> · ' +
         '<button class="link" data-batch-cancel>取消</button>')
      : ('点场次打开工作台 · 数据来自本地引擎 · <button class="link" data-open-trash>回收站' +
         (S.trashCount ? ' ' + S.trashCount : '') + '</button>');
    if (!S.sessions.length) {
      setList('<div class="side-empty">还没有场次。<br>去「收听」加一个直播间，出字后这里会出现。' +
        '<br><button class="btn sm" data-demo="1">加载演示数据</button></div>');
    } else {
      setList(S.sessions.slice(0, 100).map((s) => {
        const picked = S.batchSel.has(s.id);
        const on = !S.batchMode && s.id === S.selectedSession ? ' on' : '';
        const date = fmtClock(s.started_at);
        const dur = fmtDur((s.duration_s != null ? s.duration_s : s.elapsed) || 0);
        const n = s.n_utterances != null ? s.n_utterances : 0;
        let tag = '';
        if (s.is_demo) tag = '<span class="room-tag demo">演示</span>';
        else if (s.audio_state === 'missing') tag = '<span class="room-tag missing">录音丢失</span>';
        else if (s.audio_state === 'none') tag = '<span class="room-tag none">仅文字</span>';
        else if (s.audio_state === 'live') tag = '<span class="room-tag live">录制中</span>';
        else if (s.quality && s.quality.duplicate_of) tag = '<span class="room-tag dup">重复</span>';
        const actions = S.batchMode ? '' : ('<span class="row-actions">' +
          (s.audio_state === 'ready'
            ? '<span class="row-act" data-play="' + esc(s.id) + '" title="从开头播放">▶</span>'
            : '') +
          '<span class="row-act" data-more="session" data-id="' + esc(s.id) + '" title="更多操作">⋯</span>' +
          '</span>');
        return '<button class="room-row' + on + (picked ? ' picked' : '') +
          '" data-session="' + esc(s.id) + '">' +
          (S.batchMode
            ? '<span class="batch-check' + (picked ? ' on' : '') + '"></span>'
            : '<span class="room-ch">' + (s.audio_state === 'live' ? 'LIVE' : '已收') + '</span>') +
          '<span class="room-main"><span class="room-name">' + esc(s.title || '未命名场次') + '</span>' +
          '<span class="room-sub">' + esc(date) + ' · ' + n + ' 条' + tag + '</span></span>' +
          '<span class="room-time">' + esc(dur) + '</span>' + actions + '</button>';
      }).join(''));
    }
    return;
  }

  tools.innerHTML = '';
  setList('');
}

function renderMultiBar() {
  const bar = $('#multiBar');
  if (!bar) return;
  const live = liveRooms();
  // 标注台是全高工作台，这条条会把画面挤矮；那一页的右栏仍有「进入记录卡」。
  if (S.view !== 'listen' || S.stage === 'annotate' || live.length < 2) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  $('#multiBarText').textContent = live.length + ' 个直播间正在记录';
  // 每条路一个 CH 按钮：点谁就进谁的记录卡。最多 5 路，一行放得下。
  // 轮询每 2 秒会重绘舞台；内容没变就不重建，别把键盘焦点打断。
  const wrap = $('#multiRooms');
  const born = live.map((r) => r.id + '·' + roomCh(r) + '·' + roomName(r)).join('|');
  if (wrap.dataset.born !== born) {
    wrap.dataset.born = born;
    wrap.innerHTML = live.map((r) => {
      const on = r.id === S.selectedId ? ' on' : '';
      return '<button class="mb-room' + on + '" data-mb-room="' + esc(r.id) + '"' +
        ' title="进入「' + esc(roomName(r)) + '」的记录卡">' +
        '<i></i>' + esc(roomCh(r)) + '</button>';
    }).join('');
  } else {
    $$('#multiRooms .mb-room').forEach((b, i) => {
      b.classList.toggle('on', live[i] && live[i].id === S.selectedId);
    });
  }
  const cur = selectedRoom();
  const go = $('#multiBarGo');
  go.disabled = !cur || !canEnterHud(cur);
  go.textContent = cur && canEnterHud(cur) ? '进入 ' + roomCh(cur) + ' 记录卡' : '进入记录卡';
}

/* 多路提示里那个「选一路」：把注意力落回舞台上方那条 picker，
   不另开对话框——用户已经在这页了，多一个浮层只是多一次关闭。 */
function pickMultiRoom() {
  const bar = $('#multiBar');
  if (!bar) return;
  // 标注页没有这条条（见 renderMultiBar）；先退回信号舞台再高亮。
  if (S.stage === 'annotate') setStage('signal');
  renderMultiBar();
  bar.classList.remove('flash');
  // 重新触发一次高亮：连续点两次也要有反馈。
  void bar.offsetWidth;
  bar.classList.add('flash');
  const first = $('#multiRooms .mb-room');
  if (first) first.focus({ preventScroll: true });
  clearTimeout(pickMultiRoom._t);
  pickMultiRoom._t = setTimeout(() => bar.classList.remove('flash'), 1600);
}

function roomRowHtml(r) {
  const st = stateInfo(r);
  const s = sessionFor(r);
  const on = r.id === S.selectedId ? ' on' : '';
  const dur = s ? fmtDur(s.elapsed) : '';
  const tail = st.k === 'live'
    ? (s ? ' · ' + fmtDur(s.elapsed) : '')
    : (r.error ? ' · ' + esc(String(r.error).slice(0, 26)) : '');
  const nNew = st.k === 'live' ? unclaimedCount(s) : 0;
  const sub = '<span class="state ' + st.k + '"><i></i>' + st.label + '</span>' +
    (tail ? '<span class="sub-text">' + tail + '</span>' : '') +
    (nNew ? '<span class="room-tag new">新声音' + (nNew > 1 ? ' ' + nNew : '') + '</span>' : '');
  return '<button class="room-row' + on + '" data-room="' + esc(r.id) + '">' +
    '<span class="room-ch">' + roomCh(r) + '</span>' +
    '<span class="room-main"><span class="room-name">' + esc(roomName(r)) + '</span>' +
    '<span class="room-sub">' + sub + '</span></span>' +
    '<span class="room-time">' + esc(dur) + '</span>' +
    '<span class="row-actions"><span class="row-act" data-more="room" data-id="' +
    esc(r.id) + '" title="更多操作">⋯</span></span></button>';
}

function renderStage() {
  const room = selectedRoom();
  const st = room ? stateInfo(room) : null;
  const isWall = S.stage === 'wall';
  const isAnnot = S.stage === 'annotate';
  const isPage = S.stage === 'page';

  $('#stageCh').textContent = room ? roomCh(room) : 'CH--';
  $('#stageName').textContent = room ? roomName(room) : '选择一个直播间';
  $('#stageMark').textContent = room ? roomCh(room) : 'CH--';
  $('#btnStop').disabled = !room || st.k === 'stopped';
  ['#stagePause', '#stageMarkBtn'].forEach((sel) => {
    const b = $(sel);
    if (b) b.hidden = !room || isWall || isAnnot;
  });
  // 多路时导航栏的「记录」会被拦回收听页（见 switchView 的多路分支），
  // 所以入口放在舞台上方那条常驻条里（renderMultiBar），这里只留语义。
  renderMultiBar();
  $('#btnWall').textContent = isWall ? '信号' : '监控墙';
  $$('#stageSeg button').forEach((b) => {
    b.classList.toggle('on', !isWall && b.dataset.stage === S.stage);
    b.setAttribute('aria-selected', String(!isWall && b.dataset.stage === S.stage));
  });

  let meta = '—';
  if (room) {
    const s = sessionFor(room);
    meta = st.label + ' · ' + captureLabel(room) + (s ? ' · ' + fmtDur(s.elapsed) : '');
    if (isAnnot) {
      const a = annotStats();
      meta += ' · ' + a.claimed + ' 个已认领 / ' + a.todo + ' 个待认领';
    }
  }
  $('#stageMeta').textContent = meta;

  // 收听页的实时监视条：大计时、电平、采集方式与标记。
  const monitor = $('#monitorNow');
  if (monitor) {
    const showMonitor = !!room && !isWall && !isAnnot && !isPage;
    monitor.hidden = !showMonitor;
    if (showMonitor) {
      const s = sessionFor(room) || {};
      $('#detailTimer').textContent = fmtHudTimer(s.elapsed || 0);
      const badge = $('#detailBadge');
      badge.className = 'live-badge' + (st.k === 'live' ? (s.paused ? ' paused' : '') : ' done');
      $('#detailBadgeText').textContent = s.paused ? '已暂停' : st.label;
      $('#stagePause').textContent = s.paused ? '继续' : '暂停';
      $('#detailMode').textContent = captureLabel(room);
      $('#detailStart').textContent = fmtClock(s.started_at || s.started_utc) || '—';
      $('#detailLines').textContent = (s.n_utterances != null ? s.n_utterances : 0) + ' 条';
      const level = $('#levelMeter');
      if (level && !level.children.length) {
        level.innerHTML = Array.from({ length: 16 }, (_, i) => '<i style="--i:' + i + '"></i>').join('');
      }
      if (level) level.classList.toggle('paused', !(st.k === 'live' && !s.paused));
      const marksWrap = $('#marksWrap');
      const marks = Array.isArray(s.marks) ? s.marks : [];
      marksWrap.hidden = !marks.length;
      if (marks.length) {
        $('#marksList').innerHTML = marks.slice(-6).map((m) => {
          const t = Number(m.t || 0);
          const note = m.note ? ' ' + String(m.note).slice(0, 8) : '';
          return '<button class="mark-chip" data-mark-seek="' + t + '">' + fmtDur(t) + esc(note) + '</button>';
        }).join('');
      }
    }
  }

  const wall = $('#wall');
  const annot = $('#annot');
  const scope = $('#scope');
  const empty = $('#stageEmpty');
  const err = $('#stageError');
  const badge = $('#stageBadge');
  const stats = $('#stageStats');

  wall.hidden = !isWall || !room;
  annot.hidden = !isAnnot || !room;
  scope.style.display = (isWall || isAnnot || isPage || !room) ? 'none' : '';
  empty.hidden = !!room;
  $('#stageMark').hidden = !room || isWall || isAnnot || isPage;
  err.hidden = !room || isWall || isAnnot || isPage || st.k !== 'failed';
  badge.hidden = !room || isWall || isAnnot || isPage || !S.pageOpen;
  stats.hidden = !room || isWall || isAnnot || isPage;
  if (S.pageOpen && !isAnnot && !isPage) {
    badge.className = 'stage-badge live';
    badge.innerHTML = '<span class="dot"></span>页面模式 · 采集仍在引擎里跑';
  }
  if (room && !isWall && !isAnnot && !isPage) {
    const s = sessionFor(room);
    const lag = s && s.last_chunk_ago_s != null ? '最后一块 ' + Number(s.last_chunk_ago_s).toFixed(1) + 's 前' : '等待音频块';
    const n = s && s.n_utterances != null ? s.n_utterances : 0;
    stats.textContent = fmtDur(s ? s.elapsed : 0) + ' · ' + n + ' 条 · ' + lag;
  }
  if (err.hidden === false && room) {
    $('#stageErrTitle').textContent = st.k === 'failed' ? '这一路出了点问题' : '这一路没有在采集';
    $('#stageErrText').textContent = room.error || '采集端没有回报状态。可以先重试；仍然失败就去「设置」看诊断。';
  }
  if (isWall) renderWall();
  if (!isAnnot && S.spkPop) closeSpeakerPop();
  if (isAnnot && room) { renderAnnot(); applyAnnotLayout(); }
}

let wallWaveSeed = 0;
function wavePath(seed, w, h) {
  const mid = h / 2;
  let d = 'M0 ' + mid.toFixed(1);
  for (let x = 0; x <= w; x += 4) {
    const p = x / w;
    const a = Math.sin(p * 22 + seed) * 0.34 + Math.sin(p * 61 + seed * 1.7) * 0.16;
    const env = Math.exp(-Math.pow((p - 0.5) * 2.4, 2));
    const y = mid - a * env * (h * 0.42);
    d += ' L' + x + ' ' + y.toFixed(1);
  }
  return d;
}
function renderWall() {
  const wall = $('#wall');
  if (!S.rooms.length) {
    wall.innerHTML = '<div class="side-empty" style="grid-column:1/-1;padding:40px">还没有直播间。<br>先添加一路，再回来看监控墙。</div>';
    return;
  }
  wall.innerHTML = S.rooms.map((r, i) => {
    const st = stateInfo(r);
    const s = sessionFor(r);
    const on = r.id === S.selectedId ? ' on' : '';
    const name = esc(roomName(r));
    const line = s && s.last_line ? s.last_line : (r.error || '—');
    return '<button class="wall-card' + on + '" data-room="' + esc(r.id) + '">' +
      '<span class="wc-top"><span class="wc-ch">' + roomCh(r) + '</span>' +
      '<span class="wc-name">' + name + '</span>' +
      '<span class="wc-state state ' + st.k + '"><i></i>' + st.label + '</span></span>' +
      '<span class="wc-wave"><svg viewBox="0 0 220 54" preserveAspectRatio="none">' +
      '<path d="' + wavePath(i + 1.3, 220, 54) + '" fill="none" stroke="' +
      (st.k === 'live' ? 'rgba(132,214,178,.72)' : 'rgba(143,180,217,.28)') +
      '" stroke-width="1.3"/></svg></span>' +
      '<span class="wc-foot"><span>' + fmtDur(s ? s.elapsed : 0) + '</span>' +
      '<span class="grow">' + esc(String(line).slice(0, 34)) + '</span></span></button>';
  }).join('') +
    '<button class="wall-card wall-add" data-add="1" title="添加直播间">' +
    '<span class="wa-plus">＋</span><span class="wa-title">添加一路直播间</span>' +
    '<span class="wa-hint">粘贴链接，开始听</span></button>';
}

/* ================================================================ 标注模式 */
function annotStats() {
  const list = annotSpeakers();
  let total = 0, green = 0, amber = 0, claimed = 0, todo = 0;
  list.forEach((sp) => {
    const k = spkKind(sp);
    total += sp.seconds;
    if (k === 'confirmed' || k === 'auto' || k === 'role') { green += sp.seconds; claimed += 1; }
    else if (k === 'candidate') { amber += sp.seconds; claimed += 1; }
    else if (k === 'unknown' || k === 'rejected') todo += 1;
  });
  const pct = total > 0 ? Math.min(100, Math.round(100 * (green + amber) / total)) : 0;
  const greenPct = total > 0 ? Math.min(100, Math.round(100 * green / total)) : 0;
  const amberPct = total > 0 ? Math.min(100 - greenPct, Math.round(100 * amber / total)) : 0;
  return { list, total, green, amber, claimed, todo, pct, greenPct, amberPct, n: list.length };
}
function spkCls(kind, sp) {
  if (sp && /^主播/.test(sp.label) &&
      (kind === 'confirmed' || kind === 'auto' || kind === 'role')) return 'host';
  return ({ confirmed: 'known', auto: 'auto', role: 'role', candidate: 'sus',
            unknown: 'unknown', rejected: 'rejected' })[kind] || 'unknown';
}
function spkById(id) {
  return annotSpeakers().find((s) => s.id === id) || null;
}

function renderAnnot() {
  if (S.stage !== 'annotate') return;
  const room = selectedRoom();
  if (!room) return;
  const a = annotStats();
  const total = S.sessInfo && S.sessInfo.n_utterances != null
    ? S.sessInfo.n_utterances : S.lines.length;
  $('#txCount').textContent = String(total);
  $('#txTodoN').textContent = String(a.todo);
  $$('#txTabs .tx-tab').forEach((b) => b.classList.toggle('on', b.dataset.tab === S.annotTab));
  $('#txAuto').classList.toggle('on', S.annotAuto);
  renderAnnotFoot(room);

  // 正在认人时先别重画行：一重画弹层就丢了，等用户选完再继续。
  if (S.spkPop) return;

  const map = spkMap();
  const todoIds = new Set(a.list.filter((sp) => {
    const k = spkKind(sp);
    return k === 'unknown' || k === 'rejected';
  }).map((sp) => sp.id));
  const first = a.list.find((sp) => todoIds.has(sp.id) &&
    !S.annotSkipped.has(sp.id) && sp.seconds >= 3);
  renderAnnotBanner(first);

  let rows = S.lines;
  if (S.annotTab === 'todo') rows = rows.filter((r) => todoIds.has(String(r.speaker_id || '')));

  const html = rows.map((r) => {
    const id = String(r.speaker_id || '');
    const sp = map.get(id);
    const kind = sp ? spkKind(sp) : 'unknown';
    const label = sp ? sp.label : (r.speaker_label || id || '说话人');
    const high = r.value_tag === 'high' || r.value_tag === 'important';
    const t = r.t_start != null ? fmtDur(r.t_start) : (r.t != null ? fmtDur(r.t) : '');
    const score = kind === 'candidate' && sp && sp.score != null
      ? '<span class="score">疑似 ' + sp.score.toFixed(2) + '</span>' : '';
    return '<div class="trow' + (high ? ' high' : '') + '">' +
      '<span class="tt">' + esc(t) + '</span>' +
      '<button class="spk ' + spkCls(kind, sp) + '" data-spk-id="' + esc(id || label) + '" title="点一下认人">' +
        '<i style="background:' + (sp ? spkColor(id) : 'currentColor') + '"></i>' +
        esc(label) + score + '</button>' +
      '<span class="tx">' + esc(r.text || '') + '</span></div>';
  }).join('');

  const partial = S.partial && S.partial.text && S.annotTab === 'all'
    ? '<div class="trow partial"><span class="tt">' + esc(fmtDur(S.partial.t_start || 0)) +
      '</span><span class="spk">识别中</span><span class="tx">' + esc(S.partial.text) + '</span></div>'
    : '';

  const emptyMsg = !room.session_id
    ? '这一路还没有建立会话。等开始出字后再看。'
    : (S.annotTab === 'todo' ? '没有待认领的声音了。' : '还在等第一句。页面开始出声后，这里会一条条出现。');

  const scroll = $('#txScroll');
  if (!scroll) return;
  const keep = scroll.scrollTop;
  const nearBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 48;
  scroll.innerHTML = html + partial +
    ((!html && !partial) ? '<div class="tx-empty">' + emptyMsg + '</div>' : '');
  if (S.annotAuto && (nearBottom || !html)) scroll.scrollTop = scroll.scrollHeight;
  else scroll.scrollTop = keep;
}

function renderAnnotBanner(first) {
  const el = $('#txBanner');
  if (!el) return;
  if (!first) { el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  el.innerHTML = '<b>' + esc(first.label || first.id) + '</b> 已说 ' +
    esc(fmtDur(first.seconds)) + '，还没认领<span class="sp"></span>' +
    '<button class="btn" data-spk-act="name" data-spk="' + esc(first.id) + '">起名</button>' +
    '<button class="btn" data-spk-act="skip" data-spk="' + esc(first.id) + '">跳过</button>';
}

function renderAnnotFoot(room) {
  const note = $('#txFootNote');
  if (!note) return;
  let text = '还没有会话';
  if (S.sessInfo) {
    if (S.sessInfo.paused) text = '已暂停转写 · 采集继续';
    else if (S.sessInfo.last_final_ago_s != null) {
      text = '最后定稿 ' + Number(S.sessInfo.last_final_ago_s).toFixed(1) + 's 前';
    } else text = '等待下一句';
  } else if (room && room.session_id) text = '已结束 · 可继续标注';
  note.textContent = text;
}

function speakerCardHtml(sp) {
  const kind = spkKind(sp);
  const skipped = S.annotSkipped.has(sp.id);
  const color = spkColor(sp.id);
  let tag = '<span class="snd-tag todo">待认领</span>';
  if (kind === 'confirmed') tag = '<span class="snd-tag ok">已确认</span>';
  else if (kind === 'auto') tag = '<span class="snd-tag ok">自动认出</span>';
  else if (kind === 'role') tag = '<span class="snd-tag role">仅角色</span>';
  else if (kind === 'candidate') {
    tag = '<span class="snd-tag sus">疑似' +
      (sp.score != null ? ' ' + sp.score.toFixed(2) : '') + '</span>';
  } else if (kind === 'rejected') tag = '<span class="snd-tag rej">不是他</span>';
  else if (skipped) tag = '<span class="snd-tag rej">已跳过</span>';

  const stateNote = kind === 'candidate' ? '声纹候选'
    : kind === 'confirmed' ? '已入人物库'
    : kind === 'auto' ? '自动比对通过'
    : kind === 'rejected' ? '本场不再自动认'
    : kind === 'role' ? '名字以后再补'
    : '未入人物库';

  let acts = '';
  const id = esc(sp.id);
  if (kind === 'candidate') {
    acts = '<button class="btn tiny amber" data-spk-act="confirm" data-spk="' + id + '">确认是 ' +
      esc(sp.label) + '</button>' +
      '<button class="btn tiny" data-spk-act="reject" data-spk="' + id + '">不是他</button>';
  } else if (kind === 'unknown') {
    acts = '<button class="btn tiny amber" data-spk-act="name" data-spk="' + id + '">起名</button>' +
      '<button class="btn tiny" data-spk-act="role" data-spk="' + id + '">只标角色</button>' +
      (skipped
        ? '<button class="btn tiny" data-spk-act="unskip" data-spk="' + id + '">取消跳过</button>'
        : '<button class="btn tiny" data-spk-act="skip" data-spk="' + id + '">跳过</button>');
  } else if (kind === 'rejected') {
    acts = '<button class="btn tiny" data-spk-act="name" data-spk="' + id + '">重新认领</button>';
  } else if (kind === 'role') {
    acts = '<button class="btn tiny amber" data-spk-act="name" data-spk="' + id + '">补名字</button>' +
      '<button class="btn tiny" data-spk-act="role" data-spk="' + id + '">改角色</button>';
  } else {
    acts = '<button class="btn tiny" data-spk-act="name" data-spk="' + id + '">改名</button>' +
      '<button class="btn tiny" data-spk-act="reject" data-spk="' + id + '">不是他</button>';
  }
  return '<div class="snd' + (kind === 'unknown' ? ' todo' : '') +
    (kind === 'rejected' ? ' rej' : '') + '">' +
    '<div class="snd-top"><i class="snd-dot" style="background:' + color + '"></i>' +
      '<span class="snd-name">' + esc(sp.label) + '</span>' + tag + '</div>' +
    '<div class="snd-meta"><span>' + fmtDur(sp.seconds) + '</span><span>' +
      sp.count + ' 句</span><span>' + stateNote + '</span></div>' +
    (acts ? '<div class="snd-acts">' + acts + '</div>' : '') + '</div>';
}

function renderAnnotInspector(room) {
  const st = stateInfo(room);
  const s = sessionFor(room);
  const a = annotStats();
  const cards = a.list.length ? a.list.map(speakerCardHtml).join('')
    : '<div class="insp-empty">还没有分出说话人。第一句定稿后会在这里出现。</div>';
  const hint = a.todo
    ? '下游：总结 / 高光已带名字；<b>导出前建议补完</b>。未确认的“疑似”不会写成确定事实。'
    : (a.n ? '本场声音都认领了。总结、高光、导出可以直接用名字。' : '等第一句定稿后开始统计。');
  const kv = [];
  kv.push(['状态', '<b class="state ' + st.k + '" style="display:inline-flex"><i></i>' + st.label + '</b>']);
  kv.push(['采集', esc(captureLabel(room))]);
  kv.push(['会话', room.session_id ? esc(shortSid(room.session_id)) : '—']);
  kv.push(['时长', s ? fmtDur(s.elapsed) : '—']);
  kv.push(['字幕', (s && s.n_utterances != null ? s.n_utterances : S.lines.length) + ' 条']);
  if (s) {
    kv.push(['最后一块', s.last_chunk_ago_s != null
      ? Number(s.last_chunk_ago_s).toFixed(1) + 's 前' : '—']);
    kv.push(['质量', (s.dropped || 0) + ' 丢块 · ' + (s.queued || 0) + ' 排队']);
  } else {
    kv.push(['回放', room.session_id ? '最后一次会话' : '—']);
  }
  const tr = trafficFor(room);
  if (tr && (Number(tr.platform_bytes) > 0 || tr.source === 'estimate')) {
    kv.push(['平台下行', '↓ ' + fmtMbps(tr.platform_mbps) + ' · ' + fmtMBPerHour(tr.platform_mb_per_hour)]);
  }
  if (tr && Number(tr.audio_bytes) > 0) {
    kv.push(['音频上行', '↑ ' + Math.round(Number(tr.audio_kbps) || 0) + ' kbps · '
      + fmtMBPerHour(tr.audio_mb_per_hour)]);
  }

  const scroll = $('#inspScroll');
  const top = scroll.scrollTop;
  scroll.innerHTML =
    '<div class="insp-section"><h3>本场声音 <span class="count">' + a.n + '</span>' +
      '<span class="grow"></span><span style="font-weight:400;letter-spacing:0;font-size:8.5px">按发言时长</span></h3>' +
      cards + '</div>' +
    '<div class="insp-section"><h3>标注进度</h3>' +
      '<div class="progress"><i style="width:' + a.greenPct + '%"></i>' +
        '<b style="width:' + a.amberPct + '%"></b></div>' +
      '<div class="pstat"><b>' + a.pct + '%</b><span>按发言时长 · 已认领 ' +
        a.claimed + '/' + a.n + ' 个声音</span></div>' +
      '<div class="phint">' + hint + '</div></div>' +
    '<div class="insp-section"><h3>这一路</h3><dl class="kv">' +
      kv.map((r) => '<dt>' + r[0] + '</dt><dd>' + r[1] + '</dd>').join('') +
      '</dl>' + (s && s.paused ? '<div class="phint"><b>已暂停</b>：采集还在，只是不再转写。</div>' : '') +
    '</div>';
  scroll.scrollTop = top;

  const acts = [];
  if (canEnterHud(room)) {
    acts.push('<button class="btn sm primary" data-act="hud" title="进入这一路的记录卡：暂停 / 标记 / 结束并生成笔记">进入记录卡</button>');
  }
  acts.push('<button class="btn sm" data-act="page">显示页面</button>');
  acts.push('<button class="btn sm" data-act="live"' + (room.session_id ? '' : ' disabled') + '>实时字幕</button>');
  if (st.k === 'failed' || st.k === 'stopped') {
    acts.push('<button class="btn sm" data-act="retry">重试</button>');
  }
  if (st.k !== 'stopped') acts.push('<button class="btn sm danger" data-act="stop">停止</button>');
  $('#inspActions').innerHTML = acts.join('');
}

function openSpeakerPop(id, anchor) {
  const sp = spkById(id);
  if (!sp) return;
  S.spkPop = { id };
  const pop = $('#spkPop');
  const kind = spkKind(sp);
  const others = annotSpeakers().filter((x) => x.id !== id).slice(0, 5);
  let html = '<div class="ph"><b>这一簇是谁说的？</b>改判后整簇生效，不是只改这一句</div>';
  if (kind === 'candidate') {
    html += '<button class="pi on" data-target-confirm="1">' +
      '<span class="dot" style="background:' + spkColor(sp.id) + '"></span>' +
      esc(sp.label) + '<span class="badge">声纹 ' +
      (sp.score != null ? sp.score.toFixed(2) : '疑似') + ' 疑似</span></button>';
  }
  html += others.map((x) => '<button class="pi" data-target="' + esc(x.id) + '">' +
    '<span class="dot" style="background:' + spkColor(x.id) + '"></span>' + esc(x.label) +
    '<span class="meta">' + fmtDur(x.seconds) + '</span></button>').join('');
  html += '<button class="pi pnew" data-new="1">＋ 新建人物…</button>' +
    '<div class="roles"><div class="lb">只标角色（不进声纹库，名字以后再补）</div>' +
    '<div class="row"><button class="r" data-role="主播">主播</button>' +
    '<button class="r" data-role="嘉宾">嘉宾</button>' +
    '<button class="r" data-role="运营">运营</button></div></div>';
  if (kind === 'candidate') {
    html += '<button class="pi danger" data-reject="1">不是这个人</button>';
  }
  html += '<div class="pf">改名 = <b>注册声纹</b> · 可在「人物」页删除</div>';
  pop.innerHTML = html;
  pop.hidden = false;
  placeSpeakerPop(anchor);
  renderAnnot();
}

function placeSpeakerPop(anchor) {
  const pop = $('#spkPop');
  if (!pop || !anchor) return;
  const r = anchor.getBoundingClientRect();
  const pr = pop.getBoundingClientRect();
  // 不往左翻：左边是原生直播页子视图，HTML 弹层会被它盖住。
  // 右边不够就压到检查器上，宁可盖住 HTML，也不要被原生视图吃掉。
  let x = Math.min(r.right + 8, window.innerWidth - pr.width - 10);
  x = Math.max(8, x);
  let y = r.top - 4;
  if (y + pr.height > window.innerHeight - 10) y = window.innerHeight - pr.height - 10;
  y = Math.max(48, y);
  pop.style.left = x + 'px';
  pop.style.top = y + 'px';
}

function closeSpeakerPop() {
  if (!S.spkPop) return;
  S.spkPop = null;
  const pop = $('#spkPop');
  if (pop) { pop.hidden = true; pop.innerHTML = ''; }
  renderAnnot();
}

async function fetchAnnotSnapshot() {
  const room = selectedRoom();
  if (!room || !room.session_id) { S.speakers = []; S.lines = []; return; }
  if (stateInfo(room).k === 'live') {
    const d = await api('/api/live/status?id=' + encodeURIComponent(room.session_id));
    S.speakers = Array.isArray(d.speakers) ? d.speakers : [];
    S.speakersSid = room.session_id;
    S.partial = d.partial || null;
    S.sessInfo = d;
    S.lines = (Array.isArray(d.utterances) ? d.utterances : []).slice(-80);
    return;
  }
  const [sp, ut] = await Promise.all([
    api('/api/speakers?session_id=' + encodeURIComponent(room.session_id)),
    api('/api/utterances?session_id=' + encodeURIComponent(room.session_id) + '&limit=120'),
  ]);
  S.speakers = Array.isArray(sp.speakers) ? sp.speakers : [];
  S.speakersSid = room.session_id;
  S.partial = null;
  S.sessInfo = null;
  S.lines = (Array.isArray(ut.rows) ? ut.rows : []).slice().sort((a, b) => {
    const ta = a.t_start != null ? a.t_start : (a.t || 0);
    const tb = b.t_start != null ? b.t_start : (b.t || 0);
    return ta - tb;
  });
}

async function refreshAnnot() {
  try { await fetchAnnotSnapshot(); } catch (e) {}
  renderAnnot();
  renderInspector();
  renderStatus();
  renderSidebar();
}

async function renameSpeaker(id, name) {
  const room = selectedRoom();
  const n = String(name || '').trim().slice(0, 40);
  if (!room || !room.session_id || !id || !n) return false;
  try {
    if (isGenericName(n)) {
      // 主播 / 嘉宾 / 运营只是角色，不进人物底库。
      await post('/api/speakers/rename', {
        session_id: room.session_id, speaker_id: id, name: n,
      });
      toast('已标为「' + n + '」角色，不注册声纹。');
    } else {
      // 走 enroll 而不是 rename：能拿到“声纹够不够注册”的结果，
      // 不会在样本不足时骗用户说已经入底库。
      const out = await post('/api/persons/enroll', {
        session_id: room.session_id, speaker_id: id, name: n,
      });
      if (out && out.ok) {
        toast('已改名为 ' + n + ' · 声纹样本 +' + (out.added || 0) + '。');
      } else {
        await post('/api/speakers/rename', {
          session_id: room.session_id, speaker_id: id, name: n,
        });
        toast('已改名为 ' + n + '；' + ((out && out.reason) || '这一簇声纹样本不够') +
          '，本次没入人物库。');
      }
    }
    await refreshAnnot();
    refreshRooms();
    return true;
  } catch (e) {
    toast('改名失败：' + e.message);
    return false;
  }
}

function promptSpeakerName(id) {
  const sp = spkById(id);
  if (!sp) return;
  const current = isGenericName(sp.label) ? '' : sp.label;
  openModal(
    '<h3>给这一簇起个名字</h3>' +
    '<p>改名会把这一簇的声纹注册进人物底库，下次这个人再开口就能被认出来。' +
    '只想标角色就取消，在弹层里选「主播 / 嘉宾 / 运营」。</p>' +
    '<input class="modal-input" id="spkName" maxlength="40" placeholder="真实姓名或常用昵称" value="' +
    esc(current) + '" autocomplete="off" spellcheck="false">' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn primary" id="spkNameOk">保存并注册</button></div>',
    (sheet) => {
      const input = $('#spkName', sheet);
      const ok = $('#spkNameOk', sheet);
      setTimeout(() => { input.focus(); input.select(); }, 30);
      const submit = async () => {
        const name = (input.value || '').trim();
        if (!name) { toast('名字不能为空。'); return; }
        if (isGenericName(name)) {
          toast('「' + name + '」是角色标签，请用「只标角色」。');
          return;
        }
        ok.disabled = true;
        const done = await renameSpeaker(id, name);
        ok.disabled = false;
        if (done) closeModal();
      };
      ok.onclick = submit;
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); submit(); }
      });
    });
}

async function confirmSpeaker(id) {
  const sp = spkById(id);
  if (!sp) return;
  const name = isGenericName(sp.label) ? (sp.personName || '') : sp.label;
  closeSpeakerPop();
  if (!name) { promptSpeakerName(id); return; }
  await renameSpeaker(id, name);
}

async function rejectSpeaker(id) {
  const room = selectedRoom();
  if (!room || !room.session_id || !id) return;
  closeSpeakerPop();
  try {
    await post('/api/live/reject_hint', { session_id: room.session_id, speaker_id: id });
    toast('已排除，本场不再自动认这个人。');
    await refreshAnnot();
    refreshRooms();
  } catch (e) {
    toast('操作失败：' + e.message);
  }
}

function skipSpeaker(id) {
  S.annotSkipped.add(id);
  closeSpeakerPop();
  renderAnnot();
  renderInspector();
}

function unskipSpeaker(id) {
  S.annotSkipped.delete(id);
  renderAnnot();
  renderInspector();
}

async function mergeSpeakerInto(srcId, target) {
  const src = spkById(srcId);
  if (!src || !target || src.id === target.id) return;
  const ks = spkKind(src);
  const kt = spkKind(target);
  // 两个都已经有身份时，合并是“改判一个人”，不是修正一个未知簇：
  // 多问一句，避免手一滑把已经确认的人并没了。
  if (isClaimed(ks) && isClaimed(kt) && src.label !== target.label) {
    closeSpeakerPop();
    openModal(
      '<h3>把「' + esc(src.label) + '」并进「' + esc(target.label) + '」？</h3>' +
      '<p>这一簇的 ' + src.count + ' 句会改判给「' + esc(target.label) +
      '」，本场不再保留「' + esc(src.label) + '」。' +
      '如果两边其实是两个人，请取消，改用「不是他」或重新起名。</p>' +
      '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
      '<button class="btn danger" data-mconfirm="merge">确认合并</button></div>',
      (sheet) => {
        sheet.querySelector('[data-mconfirm="merge"]').onclick = () => {
          closeModal();
          doMergeSpeaker(src.id, target);
        };
      });
    return;
  }
  doMergeSpeaker(src.id, target);
}

async function doMergeSpeaker(srcId, target) {
  const room = selectedRoom();
  if (!room || !room.session_id || !target || !srcId || srcId === target.id) return;
  closeSpeakerPop();
  try {
    await post('/api/speakers/merge', {
      session_id: room.session_id,
      src_ids: [srcId],
      target_id: target.id,
      target_label: target.label,
    });
    toast('已把这一簇并入「' + target.label + '」。');
    await refreshAnnot();
    refreshRooms();
  } catch (e) {
    toast('合并失败：' + e.message);
  }
}

async function handleSpkAction(act, id, el) {
  if (!id) return;
  if (act === 'name' || act === 'rename') return promptSpeakerName(id);
  if (act === 'role') return openSpeakerPop(id, el || $('#txScroll'));
  if (act === 'confirm') return confirmSpeaker(id);
  if (act === 'reject') return rejectSpeaker(id);
  if (act === 'skip') return skipSpeaker(id);
  if (act === 'unskip') return unskipSpeaker(id);
}

async function markMoment() {
  const room = activeRoom();
  if (!room || !room.session_id) { toast('这一路还没有会话。'); return; }
  const bookmark = S.view === 'hud';
  try {
    const d = await post('/api/live/mark', {
      session_id: room.session_id,
      note: bookmark ? '' : '标注模式打点',
      kind: bookmark ? 'bookmark' : 'mark',
    });
    if (S.sessInfo && Array.isArray(d.marks)) S.sessInfo.marks = d.marks;
    const m = d.mark || {};
    const at = m.t != null ? ' ' + fmtDur(m.t) : '';
    const undo = m.id ? {
      label: '撤销',
      run: () => undoMark(room.session_id, m.id, bookmark),
    } : null;
    toast((bookmark ? '已加书签' : '已标记') + at, undo);
    if (S.view === 'hud') await pollTranscript();
  } catch (e) {
    toast((bookmark ? '加书签失败：' : '打点失败：') + e.message);
  }
}
async function undoMark(sid, markId, bookmark) {
  try {
    await api('/api/live/mark?session_id=' + encodeURIComponent(sid) +
      '&id=' + encodeURIComponent(markId), { method: 'DELETE' });
    toast(bookmark ? '已撤销书签' : '已撤销标记');
    await pollTranscript();
  } catch (e) {
    toast('撤销失败：' + e.message);
  }
}
async function saveHudHighlight(h) {
  if (!h || !S.hudSid) return;
  try {
    await post('/api/highlights/save', {
      session_id: S.hudSid, t0: h.t0, t1: h.t1, quote: h.quote || '',
      kind: h.kind || 'info', speaker: h.speaker || '', score: h.score || 0,
      why: h.why || '',
    });
    S.hudHi.saved.add(h.id);
    renderHudHighlight();
    toast('已收藏这段高光，笔记里会标「你收藏的」。');
  } catch (e) {
    toast('收藏失败：' + e.message);
  }
}

async function togglePause() {
  const room = activeRoom();
  if (!room || !room.session_id) return;
  const s = S.sessInfo || sessionFor(room) || {};
  const paused = !s.paused;
  try {
    await post('/api/live/pause', { session_id: room.session_id, paused });
    toast(paused ? '已暂停，这段不会被记录。' : '已继续记录。');
    if (S.view === 'hud') await pollTranscript();
    else await refreshAnnot();
  } catch (e) {
    toast('操作失败：' + e.message);
  }
}

function renderInspector() {
  // 工作台的右栏是 AI 总结，由 renderWbSummary 自己维护；
  // 房间轮询每 2 秒会调 renderAll，不能在这里重建它，否则滚动位置会被重置。
  if (S.view === 'workbench') return;
  const room = selectedRoom();
  const show = S.view === 'listen' && !!room;
  $('#inspector').classList.toggle('placeholder', !show);
  if (!show) {
    $('#inspKicker').textContent = '尚未选择';
    $('#inspTitle').textContent = '选择一个直播间';
    $('#inspSub').textContent = '右边会显示这一路的实时状态和字幕。';
    $('#inspScroll').innerHTML = '<div class="insp-empty">从左边选一路，或先添加一个直播间。<br><br>收听页只负责“看”和“控制”；采集、转写、保存都在本地引擎里，切到别的页面不会中断。</div>';
    $('#inspActions').innerHTML = '';
    return;
  }
  const st = stateInfo(room);
  const s = sessionFor(room);
  $('#inspKicker').textContent = roomCh(room) + ' · ' + st.label;
  $('#inspTitle').textContent = roomName(room);
  $('#inspSub').textContent = (hostOf(room.resolved_url || room.url) || '本地直播间') +
    (room.session_id ? ' · ' + shortSid(room.session_id) : '');

  if (S.stage === 'annotate') {
    renderAnnotInspector(room);
    return;
  }

  const rows = [];
  rows.push(['状态', '<b class="state ' + st.k + '" style="display:inline-flex"><i></i>' + st.label + '</b>']);
  rows.push(['采集', esc(captureLabel(room))]);
  rows.push(['会话', room.session_id ? esc(shortSid(room.session_id)) : '—']);
  rows.push(['时长', s ? fmtDur(s.elapsed) : '—']);
  if (s) {
    rows.push(['字幕', String(s.n_utterances || 0) + ' 条']);
    rows.push(['最后一块', s.last_chunk_ago_s != null ? Number(s.last_chunk_ago_s).toFixed(1) + 's 前' : '—']);
    if (s.last_line) rows.push(['最后一句', esc(String(s.last_line).slice(0, 60))]);
  }
  // 流量放在「这一路」里：它是实时遥测，跟着房间走，不是设置/日志里的历史账。
  const tr = trafficFor(room);
  const hasDown = tr && (Number(tr.platform_bytes) > 0 || tr.source === 'estimate');
  if (hasDown) {
    rows.push(['平台下行', '<span title="本次已收 ' + fmtBytes(tr.platform_bytes)
      + '">↓ ' + fmtMbps(tr.platform_mbps) + ' · ' + fmtMBPerHour(tr.platform_mb_per_hour) + '</span>']);
  }
  if (tr && Number(tr.audio_bytes) > 0) {
    rows.push(['音频上行', '<span title="采集端推给本机服务的音频；同机运行时走回环，不占宽带出口">↑ '
      + Math.round(Number(tr.audio_kbps) || 0) + ' kbps · ' + fmtMBPerHour(tr.audio_mb_per_hour) + '</span>']);
  }
  if (hasDown) {
    rows.push(['流量计量', '<span title="' + esc(tr.note || '')
      + '">' + trafficSourceLabel(tr.source) + '</span>']);
  }
  if (room.error) rows.push(['错误', '<span style="color:var(--red)">' + esc(String(room.error).slice(0, 80)) + '</span>']);

  const lines = S.lines.slice(-12).map((r) => {
    const high = r.value_tag === 'high' || r.value_tag === 'important';
    const t = r.t_start != null ? fmtDur(r.t_start) : (r.t != null ? fmtDur(r.t) : '');
    return '<div class="tline' + (high ? ' high' : '') + '">' +
      '<span class="tt">' + esc(t) + '</span>' +
      '<span class="tx">' + esc(r.text || '') + '</span></div>';
  }).join('');

  const scroll = $('#inspScroll');
  const top = scroll.scrollTop;
  scroll.innerHTML =
    '<div class="insp-section"><h3>这一路</h3><dl class="kv">' +
    rows.map((r) => '<dt>' + r[0] + '</dt><dd>' + r[1] + '</dd>').join('') +
    '</dl></div>' +
    '<div class="insp-section"><h3>实时字幕 <span class="count">' + (s ? (s.n_utterances || 0) : 0) + '</span></h3>' +
    (lines || '<div class="insp-empty">' + (room.session_id ? '还在等第一句。页面开始出声后，这里会一条条出现。' : '这一路还没有建立会话。启动完成后再看。') + '</div>') +
    '</div>';
  scroll.scrollTop = top;

  const acts = [];
  if (canEnterHud(room)) {
    acts.push('<button class="btn sm primary" data-act="hud" title="进入这一路的记录卡：暂停 / 标记 / 结束并生成笔记">进入记录卡</button>');
  }
  acts.push('<button class="btn sm" data-act="page">' + (S.stage === 'page' ? '返回信号' : '显示页面') + '</button>');
  acts.push('<button class="btn sm" data-act="live"' + (room.session_id ? '' : ' disabled') + '>实时字幕</button>');
  if (st.k === 'failed' || st.k === 'stopped') acts.push('<button class="btn sm" data-act="retry">重试</button>');
  if (st.k === 'failed' || st.k === 'stopped') acts.push('<button class="btn sm danger" data-act="remove">移除</button>');
  if (st.k !== 'stopped') acts.push('<button class="btn sm danger" data-act="stop">停止</button>');
  $('#inspActions').innerHTML = acts.join('');
}

function renderStatus() {
  const el = $('#engineStatus');
  el.className = 'engine ' + (S.online ? 'ok' : 'bad');
  const port = API ? (API.match(/:(\d+)/) || [])[1] : '';
  el.innerHTML = '<i></i><span>' + (S.online
    ? '本地引擎 · 127.0.0.1' + (port ? ':' + port : '')
    : '引擎未连接 · 重试中') + '</span>';
  // 连不上时把地址和排查路径放进 tooltip：最常见的原因是引擎没起来、
  // 端口换了，或页面 origin 被引擎的 CORS 白名单挡掉（tauri dev 的 devUrl）。
  el.title = S.online
    ? '引擎 ' + (API || '—')
    : (API
      ? '请求 ' + API + '/api/rooms 失败。检查：引擎是否启动、端口是否变化、托盘 → 诊断里的日志。'
      : '没有拿到引擎地址（window.__LN_ENGINE__ 为空）。');
  $('#statRooms').textContent = S.rooms.length + ' 个直播间';
  $('#statLive').textContent = liveRooms().length + ' 路在听';
  const tt = liveTrafficTotals();
  const statTr = $('#statTraffic');
  statTr.hidden = !tt.n;
  statTr.textContent = tt.n
    ? '↓ ' + fmtMBPerHour(tt.down) + ' · ↑ ' + fmtMBPerHour(tt.up) : '';
  statTr.title = tt.n
    ? '在听 ' + tt.n + ' 路折合每小时；音频上行同机运行时走回环，不占宽带出口' : '';
  $('#liveCount').textContent = String(liveRooms().length);
  const pill = $('#recPill');
  pill.classList.toggle('idle', liveRooms().length === 0);
  const dot = $('#recDot');
  dot.classList.toggle('pulse', liveRooms().length > 0);
  $('#statHint').textContent = S.online
    ? (S.view === 'hud'
      ? '关窗后继续录 · 托盘可以暂停 / 结束'
      : (S.view === 'listen' && S.stage === 'annotate'
        ? '标注模式 · 已认领 ' + annotStats().claimed + '/' + annotStats().n + ' 人 · 关窗后继续录音'
        : '关窗后继续录音 · Ctrl K 命令'))
    : '检查引擎日志或托盘诊断';
  if (S.view === 'hud') {
    const hr = hudRoom();
    const sub = S.finish ? '正在收尾'
      : (!hr ? '未在记录' : (liveRooms().length > 1 ? liveRooms().length + ' 路在记录' : '正在记录'));
    $('#crumb').innerHTML = '<b>记录</b><i>/</i><span>' + esc(sub) + '</span>';
    document.title = 'LiveNotes · ' + (S.finish ? '正在收尾' : (hr ? '记录中' : '记录'));
  }
}

/* ================================================================ 工作台（原生）
   一场 = 侧栏场次 + 时间轴 + 说话人筛选 + 字幕 + 底部播放器 + 右侧 AI 总结。
   数据全部来自本地引擎；字幕行可以点时间码回到原文。 */
const WB_TAGS = {
  substantive: ['观点', 'opinion'],
  question_consult: ['提问', 'question'],
  host_control: ['信息', 'info'],
  chitchat: ['信息', 'info'],
  ad: ['广告', 'emotion'],
};
const wbHigh = (r) => r && (r.value_tag === 'high' || r.value_tag === 'important');
function wbSpeakerList() {
  return (S.wb.speakers || []).map(normSpeaker).filter((s) => s.id)
    .sort((a, b) => b.count - a.count || b.seconds - a.seconds);
}
function wbSpkLabel(id) {
  const sp = wbSpeakerList().find((x) => x.id === String(id));
  if (!sp) return String(id || '说话人');
  return sp.personName || sp.label || sp.id;
}
function wbLineHtml(r) {
  const t = Number(r.t_start) || 0;
  const tag = WB_TAGS[r.category] || ['信息', 'info'];
  const high = wbHigh(r);
  const sp = wbSpkLabel(r.speaker_id);
  return '<article class="line" data-line-id="' + esc(r.id) + '" data-t="' + t + '">' +
    '<button class="line-time" data-seek="' + t + '">' + fmtDur(t) + '</button>' +
    '<div class="line-body"><div class="line-head">' +
    '<span class="line-spk" style="--spk:' + spkColor(r.speaker_id) + '"><i></i>' + esc(sp) + '</span>' +
    '<span class="tag tag-' + tag[1] + '">' + tag[0] + '</span>' +
    (high ? '<span class="line-high">高价值</span>' : '') +
    '</div><p class="line-text">' + esc(r.text || '') + '</p></div></article>';
}

function renderWbHeader() {
  const wb = S.wb;
  const s = findSession(wb.sid);
  const sm = wb.summaries.content;
  $('#wbTitle').textContent = wb.sid
    ? ((s && s.title) || (sm && sm.title) || '未命名场次')
    : '工作台';
  if (!wb.sid) {
    $('#wbMeta').textContent = '选择左侧场次，查看时间轴、字幕与总结。';
  } else {
    const meta = [
      fmtClock(s && s.started_at),
      '时长 ' + fmtDur(wb.duration || (s && s.duration_s) || 0),
      (wb.total || wb.rows.length) + ' 条发言',
      wb.speakers.length + ' 位说话人',
    ].filter(Boolean);
    if (s && s.title_source) meta.push('标题 ' + wbTitleSourceLabel(s.title_source));
    $('#wbMeta').textContent = meta.join(' · ');
  }
  $('#wbExport').disabled = !wb.sid;
}

function renderWbTimeline() {
  const wb = S.wb;
  const bars = $('#wbTlBars');
  const axis = $('#wbTlAxis');
  if (!wb.rows.length) {
    bars.innerHTML = '';
    axis.innerHTML = '';
    return;
  }
  const dur = Math.max(1, wb.duration || Number(wb.rows[wb.rows.length - 1].t_end) || 1);
  const N = 44;
  const buckets = Array.from({ length: N }, (_, i) => ({ t: dur * i / N, total: 0, spk: {}, overlap: false }));
  wb.rows.forEach((r) => {
    const t = Number(r.t_start) || 0;
    const i = clamp(Math.floor(t / dur * N), 0, N - 1);
    const b = buckets[i];
    b.total += 1;
    const k = String(r.speaker_id || '?');
    b.spk[k] = (b.spk[k] || 0) + 1;
    if (r.overlap) b.overlap = true;
  });
  const max = Math.max(1, ...buckets.map((b) => b.total));
  bars.innerHTML = buckets.map((b) => {
    const top = Object.entries(b.spk).sort((a, c) => c[1] - a[1])[0];
    const h = b.total ? 18 + Math.round(72 * b.total / max) : 4;
    const color = top ? spkColor(top[0]) : 'var(--line-2)';
    const tip = fmtDur(b.t) + ' · ' + b.total + ' 条';
    return '<button class="tl-bar' + (b.overlap ? ' overlap' : '') + '" data-t="' + Math.round(b.t) +
      '" style="--h:' + h + '%;--spk:' + color + '" title="' + esc(tip) + '" aria-label="' + esc(tip) + '"></button>';
  }).join('');
  axis.innerHTML = [0, 0.25, 0.5, 0.75, 1].map((p) => '<span>' + fmtDur(dur * p) + '</span>').join('');
  $('#wbTlHint').textContent = '点击柱体定位时段 · ' + fmtDur(dur);
}

function renderWbFilters() {
  const wb = S.wb;
  const list = wbSpeakerList();
  const counts = new Map();
  wb.rows.forEach((r) => {
    const k = String(r.speaker_id || '');
    counts.set(k, (counts.get(k) || 0) + 1);
  });
  let html = '<button class="spk-chip' + (wb.spk === 'all' ? ' on' : '') + '" data-spk-filter="all">全部 <span class="n">' +
    (wb.total || wb.rows.length) + '</span></button>';
  html += list.slice(0, 10).map((sp) =>
    '<button class="spk-chip' + (wb.spk === sp.id ? ' on' : '') + '" data-spk-filter="' + esc(sp.id) + '">' +
    '<span class="spk-dot" style="--spk:' + spkColor(sp.id) + '"></span>' + esc(sp.personName || sp.label) +
    ' <span class="n">' + (counts.get(sp.id) || 0) + '</span></button>').join('');
  $('#wbSpkFilters').innerHTML = html;
  const box = $('#wbHighOnly');
  if (box) box.checked = wb.high;
}

function renderWbTranscript() {
  const wb = S.wb;
  let list = wb.rows;
  if (wb.spk !== 'all') list = list.filter((r) => String(r.speaker_id) === wb.spk);
  if (wb.high) list = list.filter(wbHigh);
  const total = wb.total || wb.rows.length;
  const cap = 3000;
  const shown = list.slice(0, cap);
  $('#wbTranscript').innerHTML = shown.map(wbLineHtml).join('');
  $('#wbEmpty').hidden = list.length > 0;
  if (!list.length) {
    $('#wbEmpty').textContent = wb.rows.length
      ? '没有符合筛选条件的发言，试试放宽条件。'
      : '本场还没有转写内容。';
  }
  $('#wbFilterMeta').textContent = list.length > cap
    ? '显示 ' + cap + ' / ' + list.length + ' 条 · 共 ' + total + ' 条'
    : '显示 ' + list.length + ' 条 · 共 ' + total + ' 条';
  const box = $('#wbHighOnly');
  if (box) box.checked = wb.high;
}

function renderWbPlayer() {
  const wb = S.wb;
  const audio = $('#wbAudio');
  const range = $('#wbRange');
  $('#wbDur').textContent = fmtDur(wb.duration || 0);
  $('#wbCur').textContent = fmtDur(wb.currentT || 0);
  $('#wbPlay').disabled = !wb.audioReady;
  range.disabled = !wb.audioReady;
  range.max = String(Math.max(0, Math.round(wb.duration || 0)));
  range.value = String(Math.max(0, Math.min(wb.duration || 0, Math.round(wb.currentT || 0))));
  const s = findSession(wb.sid);
  $('#wbNote').textContent = !wb.sid ? '点击时间码可定位'
    : (wb.audioReady ? '点击时间码可定位'
      : (s && s.audio_state === 'live' ? '录制中 · 结束后可回放' : '本场没有原始音频'));
  if (!audio) return;
  if (wb.sid && wb.audioReady) {
    if (audio.dataset.sid !== wb.sid) {
      audio.dataset.sid = wb.sid;
      audio.src = API + '/api/audio?session_id=' + encodeURIComponent(wb.sid);
    }
  } else if (audio.dataset.sid) {
    audio.pause();
    audio.removeAttribute('src');
    audio.dataset.sid = '';
    try { audio.load(); } catch (e) {}
  }
}

function renderWbAll() {
  renderWbHeader();
  renderWbTimeline();
  renderWbFilters();
  renderWbTranscript();
  renderWbPlayer();
  renderWbSummary();
}

async function openWorkbench(sid, opts) {
  opts = opts || {};
  sid = sid || '';
  const wb = S.wb;
  const changed = wb.sid !== sid;
  if (changed) {
    wb.sid = sid;
    wb.rows = [];
    wb.speakers = [];
    wb.total = 0;
    wb.duration = 0;
    wb.spk = 'all';
    wb.high = false;
    wb.summaries = {};
    wb.coach = null;
    wb.summaryScope = 'content';
    wb.titleCand = null;
    wb.titleBusy = false;
    wb.summaryError = null;
    wb.currentT = 0;
    wb.autoplay = !!opts.autoplay;
    wb.seekT = opts.t != null ? Number(opts.t) : null;
  } else {
    if (opts.autoplay) wb.autoplay = true;
    if (opts.t != null) wb.seekT = Number(opts.t);
  }
  S.selectedSession = sid;
  S.workbenchSid = sid;
  S.workbenchReady = false;
  renderWbHeader();
  renderWbSummary();
  renderSidebar();
  // 同一场再点一次（或轮询触发）不重新拉字幕，保住滚动和筛选。
  if (!changed && wb.rows.length && !opts.autoplay && opts.t == null) {
    S.workbenchReady = true;
    return;
  }
  if (!sid) {
    wb.rows = [];
    renderWbTimeline();
    renderWbFilters();
    renderWbTranscript();
    renderWbPlayer();
    S.workbenchReady = true;
    return;
  }
  const seq = ++wb.seq;
  try {
    const [u, sp, sm] = await Promise.all([
      api('/api/utterances?session_id=' + encodeURIComponent(sid) + '&limit=5000'),
      api('/api/speakers?session_id=' + encodeURIComponent(sid)).catch(() => ({ speakers: [] })),
      api('/api/summary?session_id=' + encodeURIComponent(sid) + '&scope=content').catch(() => null),
    ]);
    if (seq !== wb.seq || S.view !== 'workbench') return;
    wb.rows = (u.rows || []).slice().sort((a, b) => (Number(a.t_start) || 0) - (Number(b.t_start) || 0));
    wb.speakers = sp.speakers || [];
    wb.total = Number(u.total) || wb.rows.length;
    const s = findSession(sid);
    const last = wb.rows[wb.rows.length - 1];
    wb.duration = Math.max(0, Number(s && s.duration_s) || Number(last && (last.t_end || last.t_start)) || 0);
    if (sm && sm.ok && sm.payload) wb.summaries.content = sm.payload;
    wb.audioReady = !!(s && s.audio_state === 'ready');
    S.workbenchReady = true;
    renderWbAll();
    if (wb.autoplay || wb.seekT != null) {
      const t = wb.seekT != null ? wb.seekT : 0;
      wb.autoplay = false;
      wb.seekT = null;
      setTimeout(() => wbSeek(t, { play: true }), 80);
    }
  } catch (e) {
    if (seq !== wb.seq) return;
    S.workbenchReady = true;
    renderWbAll();
    toast('工作台加载失败：' + e.message);
  }
}

function wbNearestLine(t) {
  const nodes = $$('#wbTranscript .line');
  if (!nodes.length) return null;
  for (const el of nodes) {
    if ((Number(el.dataset.t) || 0) >= t - 0.5) return el;
  }
  return nodes[nodes.length - 1];
}
let wbFocusTimer = null;
function highlightWbLine(el) {
  if (!el) return;
  $$('#wbTranscript .line.is-focus').forEach((x) => x.classList.remove('is-focus'));
  el.classList.add('is-focus');
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  clearTimeout(wbFocusTimer);
  wbFocusTimer = setTimeout(() => el.classList.remove('is-focus'), 2600);
}
function wbSeek(t, opts) {
  const wb = S.wb;
  t = Math.max(0, Number(t) || 0);
  wb.currentT = t;
  $('#wbCur').textContent = fmtDur(t);
  const range = $('#wbRange');
  range.value = String(Math.round(t));
  if (wb.audioReady) {
    const audio = $('#wbAudio');
    try { audio.currentTime = t; } catch (e) {}
    if (opts && opts.play) audio.play().catch(() => {});
  }
  highlightWbLine(wbNearestLine(t));
}
function wbTogglePlay() {
  const wb = S.wb;
  const audio = $('#wbAudio');
  if (!wb.audioReady) { toast('本场没有原始音频，只能看文字。'); return; }
  if (audio.paused) audio.play().catch(() => toast('播放失败'));
  else audio.pause();
}

/* ---------------------------------------------------------------- 总结来源与标题 */
function wbEngineMeta(payload) {
  const eng = String((payload && payload.engine) || '');
  const kind = String((payload && payload.engine_kind)
    || (eng.indexOf('llm') === 0 ? 'llm' : (eng ? 'extractive' : 'unknown')));
  if (kind === 'llm') {
    const m = eng.match(/^llm\((.*)\)$/);
    const model = String((payload && payload.engine_model) || (m && m[1]) || '');
    return {
      cls: 'is-llm', label: 'AI 生成',
      detail: model || '模型名未记录', note: '',
    };
  }
  if (kind === 'extractive') {
    return {
      cls: 'is-local', label: '本地摘要（旧版）', detail: '不是 AI 生成',
      note: '本地抽取式已停用；重新生成会调用大模型。',
    };
  }
  return {
    cls: 'is-unknown', label: '旧版摘要', detail: '生成方式未知',
    note: '这条总结来自旧版本，无法确认是否由模型生成。',
  };
}
function wbTitleSourceLabel(src) {
  src = String(src || '');
  // auto 只代表「系统起的」；旧数据没有 title_kind，不能一概叫 AI 命名。
  return src === 'user' ? '你命名的'
    : src === 'auto' ? '自动命名'
      : src === 'auto_local' ? '本地命名'
        : src === 'legacy' ? '旧版命名' : '默认名';
}
function wbTitleBarHtml() {
  const wb = S.wb;
  const p = wb.summaries.content || {};
  const s = findSession(wb.sid) || {};
  const cand = wb.titleCand;
  const main = String((cand && cand.title) || p.title || '').trim();
  if (!main) return '';
  const cur = String(s.title || '').trim();
  const alts = (((cand && cand.alts) || p.title_alts || []) || [])
    .map((x) => String(x || '').trim())
    .filter((x) => x && x !== main && x !== cur)
    .filter((x, i, arr) => arr.indexOf(x) === i)
    .slice(0, 2);
  const applied = !cand && (!!p.title_applied || main === cur);
  const reason = String((cand && cand.reason) || p.title_reason || '');
  if (!cand && applied && !alts.length) return '';
  if (!cand && /内容太少|内容不足|只能给出人数|局部筛选/.test(reason)) return '';
  // 标题来源必须说真话：只有 title_kind=llm 才配叫「AI 标题」。
  // 旧数据没有 title_kind，auto 只写「自动标题」，不冒充 AI。
  const src = String(s.title_source || '');
  const tk = String(p.title_kind || '');
  const kicker = cand ? 'AI 标题（候选）'
    : !applied ? (tk === 'llm' ? 'AI 建议标题' : '建议标题')
      : src === 'user' ? '你命名的标题'
        : src === 'auto' ? (tk === 'llm' ? 'AI 标题' : '自动标题')
          : src === 'auto_local' ? '本地标题'
            : src === 'legacy' ? '旧版标题' : '标题';
  const srcLabel = cand ? 'AI 起的名字 · 未采用' : wbTitleSourceLabel(src);
  return '<div class="sum-titlebar">' +
    '<div class="tb-head"><span class="tb-kicker">' + kicker + '</span>' +
    '<span class="tb-src">' + esc(srcLabel) + '</span>' +
    '<span class="tb-acts">' +
    (applied ? '<span class="tb-current">当前名</span>'
      : '<button class="tb-btn" data-title-adopt="' + esc(main) + '">采用</button>') +
    '<button class="tb-btn ghost" data-title-fresh="1" title="再调一次模型换个名字（会消耗 token）">换一个</button>' +
    '</span></div>' +
    '<b class="tb-main">' + esc(main) + '</b>' +
    (alts.length ? '<div class="tb-alts">' + alts.map((a) =>
      '<button class="tb-alt" data-title-adopt="' + esc(a) + '">' + esc(a) + '</button>').join('') + '</div>' : '') +
    (cur && cur !== main && !applied
      ? '<div class="tb-note">当前名：' + esc(cur) + '；采用会覆盖它</div>' : '') +
    (reason ? '<div class="tb-note">' + esc(reason) + '</div>' : '') +
    '</div>';
}
async function adoptWbTitle(t) {
  const wb = S.wb;
  if (!wb.sid || !t) return;
  try {
    await post('/api/sessions/title/adopt', { session_id: wb.sid, title: t });
    wb.titleCand = null;
    // 面板里缓存的是「总结那次的标题快照」：采用后要同步，否则标题条还挂着
    // 旧名字和「采用」按钮，再点一次会把用户刚定的名字覆盖回去。
    const p = wb.summaries.content;
    if (p) {
      p.title = t;
      p.title_applied = true;
      p.title_source = 'user';
      p.title_alts = [];
    }
    await loadSessions();
    renderWbHeader();
    renderSidebar();
    renderWbSummary();
    toast('已命名为「' + t + '」');
  } catch (e) {
    toast('采用失败：' + e.message);
  }
}
async function freshWbTitle() {
  const wb = S.wb;
  if (!wb.sid || wb.titleBusy) return;
  wb.titleBusy = true;
  try {
    const sg = await api('/api/sessions/suggest_title?session_id=' +
      encodeURIComponent(wb.sid) + '&fresh=1');
    const ai = (sg.groups || []).find((g) => g.kind === 'ai');
    const items = ((ai && ai.items) || []).filter((x) => String(x || '').trim());
    if (!items.length) {
      toast('模型没给出新名字（可能没配好或内容太少）');
      return;
    }
    wb.titleCand = {
      title: items[0], alts: items.slice(1, 3),
      reason: '新起的名字，点「采用」才生效；换一次会消耗 token',
    };
    renderWbSummary();
  } catch (e) {
    toast('换一个失败：' + e.message);
  } finally {
    wb.titleBusy = false;
  }
}

function wbPointHtml(p) {
  const t = p && p.t_from != null ? Number(p.t_from) : 0;
  const sp = Array.isArray(p && p.speakers) ? p.speakers.slice(0, 2).join(' / ') : '';
  return '<li class="sum-point"><button class="sum-time" data-seek="' + t + '">' + fmtDur(t) + '</button>' +
    '<div><p>' + (p.title ? '<b>' + esc(p.title) + '</b> · ' : '') + esc((p && p.gist) || '') + '</p>' +
    '<div class="sum-meta">' + ((p && p.utterance_count) || 0) + ' 条' + (sp ? ' · ' + esc(sp) : '') + '</div></div></li>';
}
function wbSummaryHtml(payload, scope) {
  const err = S.wb.summaryError && S.wb.summaryError.scope === scope
    ? S.wb.summaryError.message : '';
  if (!payload) {
    const name = scope === 'time' ? '时间轴总结' : (scope === 'category' ? '分类总结' : '内容总结');
    const short = /内容太少/.test(err);
    return '<div class="sum-empty">这一场还没有' + name + '。' +
      '<br>总结只由大模型生成；本地抽取式已停用。' +
      (err ? '<div class="sum-error">' + esc(err) + '</div>' : '') +
      '<br><button class="btn sm" data-wb-gen="' + scope + '">生成总结</button>' +
      (short ? '<button class="btn sm" data-wb-gen="' + scope + '" data-engine="llm">仍用大模型生成</button>' : '') +
      '</div>';
  }
  let html = '';
  const em = wbEngineMeta(payload);
  html += '<div class="sum-engine ' + em.cls + '">' +
    '<span class="se-k">' + esc(em.label) + '</span>' +
    (em.detail ? '<span> · ' + esc(em.detail) + '</span>' : '') +
    (payload.generated_at ? '<span> · ' + esc(fmtClock(payload.generated_at)) + '</span>' : '') +
    (payload.cached ? '<span> · 缓存命中 · 0 token</span>' : '') +
    (!payload.cached && payload.usage && payload.usage.total_tokens
      ? '<span> · ' + Number(payload.usage.total_tokens) + ' tokens</span>' : '') +
    (em.note ? '<div class="se-note">' + esc(em.note) + '</div>' : '') +
    '</div>';
  if (err) html += '<div class="sum-error">' + esc(err) + '</div>';
  if (payload.overview) html += '<p class="sum-lead">' + esc(payload.overview) + '</p>';
  if (scope === 'category' && Array.isArray(payload.groups) && payload.groups.length) {
    html += payload.groups.map((g) => {
      const tag = WB_TAGS[g.category] || ['信息', 'info'];
      const pts = (g.points || []).map(wbPointHtml).join('');
      return '<div class="sum-group"><div class="category-head"><span class="tag tag-' + tag[1] + '">' +
        esc(g.label || tag[0]) + '</span><span class="n">' + (g.count || 0) + ' 条</span></div>' +
        (Array.isArray(g.topics) && g.topics.length ? '<p class="sum-more">话题：' + esc(g.topics.slice(0, 4).join(' · ')) + '</p>' : '') +
        (pts || '<p class="sum-more">这一类暂时没有可展开的要点。</p>') + '</div>';
    }).join('');
  } else if (scope === 'time' && Array.isArray(payload.points)) {
    html += payload.points.map((p) => {
      const quotes = (p.evidence || []).slice(0, 2).map((e) =>
        '<li><time>' + fmtDur(e.t_start) + '</time><span>' + esc(e.quote || '') + '</span></li>').join('');
      return '<div class="sum-group"><h3>' + esc(p.title || (fmtDur(p.t_from) + ' – ' + fmtDur(p.t_to))) + '</h3>' +
        '<p class="sum-lead">' + esc(p.gist || '') + '</p>' + (quotes ? '<ul>' + quotes + '</ul>' : '') + '</div>';
    }).join('');
  } else if (Array.isArray(payload.points) && payload.points.length) {
    html += '<ul class="sum-points">' + payload.points.map(wbPointHtml).join('') + '</ul>';
  } else {
    html += '<div class="sum-empty">这一场内容太少，暂时整理不出要点。</div>';
  }
  const meta = [];
  if (payload.stats && payload.stats.included_utterances != null) {
    meta.push(payload.stats.included_utterances + ' 条入选');
  }
  if (Array.isArray(payload.caveats) && payload.caveats.length) meta.push(payload.caveats[0]);
  if (meta.length) html += '<p class="sum-foot">' + esc(meta.join(' · ')) + '</p>';
  return html;
}
function wbCoachHtml() {
  const c = S.wb.coach;
  if (!c) {
    return '<div class="sum-empty">还没有复盘。生成后会看主持 / 嘉宾的发言量、冷场和抢话。' +
      '<br><button class="btn sm" data-wb-gen="coach">开始复盘</button></div>';
  }
  const m = (label, val) => '<div class="metric"><b>' + esc(String(val)) + '</b><span>' + label + '</span></div>';
  let html = '<div class="coach-metrics">' +
    m('主持发言', ((c.host && c.host.utterances) || 0) + ' 句') +
    m('嘉宾发言', ((c.guest && c.guest.utterances) || 0) + ' 句') +
    m('冷场', ((c.cold && c.cold.count) || 0) + ' 次') +
    m('抢话', (c.switches || 0) + ' 次') + '</div>';
  if (c.overall) html += '<p class="sum-lead">' + esc(c.overall) + '</p>';
  if (c.grade) {
    const cover = c.coverage ? Math.round((Number(c.coverage.coverage_ratio) || 0) * 100) : null;
    html += '<p class="sum-foot">评级 ' + esc(String(c.grade)) + (cover != null ? ' · 覆盖 ' + cover + '%' : '') + '</p>';
  }
  const dims = c.dimensions;
  if (dims && typeof dims === 'object') {
    const entries = Array.isArray(dims)
      ? dims.map((d, i) => [d.name || d.key || ('维度 ' + (i + 1)), d.score != null ? d.score : d.value])
      : Object.entries(dims).filter(([, v]) => typeof v === 'number' || typeof v === 'string');
    if (entries.length) {
      html += '<div class="sum-group"><h3>维度</h3><ul>' + entries.slice(0, 8).map(([k, v]) =>
        '<li><time>' + esc(String(v)) + '</time><span>' + esc(String(k)) + '</span></li>').join('') + '</ul></div>';
    }
  }
  const list = wbSpeakerList();
  const total = list.reduce((n, sp) => n + (sp.seconds || 0), 0);
  if (list.length && total > 0) {
    html += '<div class="sum-group"><h3>说话占比</h3><ul>' + list.slice(0, 6).map((sp) =>
      '<li><time>' + Math.round(100 * sp.seconds / total) + '%</time><span style="color:' + spkColor(sp.id) + '">' +
      esc(sp.personName || sp.label) + ' · ' + sp.count + ' 句</span></li>').join('') + '</ul></div>';
  }
  if (Array.isArray(c.caveats) && c.caveats.length) html += '<p class="sum-foot">' + esc(c.caveats[0]) + '</p>';
  return html;
}
function wbSummaryKicker(scope) {
  if (scope === 'coach') return '复盘';
  const p = S.wb.summaries[scope];
  if (!p) return '总结';
  const em = wbEngineMeta(p);
  if (em.cls === 'is-llm') return 'AI 总结';
  if (em.cls === 'is-local') return '本地摘要（旧版）';
  return '旧版摘要';
}
function renderWbSummary() {
  const wb = S.wb;
  const scope = wb.summaryScope;
  $('#inspKicker').textContent = wbSummaryKicker(scope);
  const s = findSession(wb.sid);
  $('#inspTitle').textContent = wb.sid ? ((s && s.title) || '本场笔记') : '本场笔记';
  $('#inspSub').textContent = wb.sid ? '要点可回溯原文 · ' + shortSid(wb.sid) : '选择一场查看总结';
  const tabs = [['content', '要点'], ['time', '时间'], ['category', '分类'], ['coach', '复盘']];
  let body = '<div class="sum-tabs">' + tabs.map(([k, l]) =>
    '<button class="sum-tab' + (scope === k ? ' on' : '') + '" data-sum-tab="' + k + '">' + l + '</button>').join('') + '</div>';
  if (!wb.sid) body += '<div class="sum-empty">从左边选一场，这里会显示 AI 总结。</div>';
  else if (scope === 'coach') body += wbCoachHtml();
  else {
    if (scope === 'content') body += wbTitleBarHtml();
    body += wbSummaryHtml(wb.summaries[scope], scope);
  }
  const scroll = $('#inspScroll');
  const top = scroll.scrollTop;
  scroll.innerHTML = body;
  scroll.scrollTop = top;
  $('#inspActions').innerHTML = wb.sid
    ? '<button class="btn primary sm" data-wb-gen="' + scope + '"' + (wb.busy ? ' disabled' : '') + '>' +
      (wb.busy ? '正在生成…' : (scope === 'coach' ? '重新复盘' : '生成总结')) + '</button>'
    : '';
}
async function loadWbSummary(scope) {
  const wb = S.wb;
  const sid = wb.sid;
  if (!sid) return;
  try {
    const d = await api('/api/summary?session_id=' + encodeURIComponent(sid) + '&scope=' + scope);
    if (!d || !d.ok || !d.payload || S.wb.sid !== sid) return;
    wb.summaries[scope] = d.payload;
    if (wb.summaryScope === scope) renderWbSummary();
  } catch (e) {}
}
async function loadWbCoach() {
  const sid = S.wb.sid;
  if (!sid || S.wb.coach) return;
  try {
    const d = await api('/api/coach?session_id=' + encodeURIComponent(sid));
    if (S.wb.sid !== sid) return;
    S.wb.coach = d;
  } catch (e) {
    if (S.wb.sid === sid) S.wb.coach = null;
  }
  if (S.wb.summaryScope === 'coach' && S.wb.sid === sid) renderWbSummary();
}
function setWbSummaryScope(scope) {
  S.wb.summaryScope = scope;
  renderWbSummary();
  if (scope === 'coach') loadWbCoach();
  else if (!S.wb.summaries[scope]) loadWbSummary(scope);
}
async function generateWbSummary(scope, engine) {
  const wb = S.wb;
  const sid = wb.sid;
  if (!sid || wb.busy) return;
  wb.busy = true;
  wb.summaryError = null;
  wb.titleCand = null;      // 重新生成后，旧的「换一个」候选就过期了
  renderWbSummary();
  try {
    if (scope === 'coach') {
      const d = await api('/api/coach?session_id=' + encodeURIComponent(sid));
      if (S.wb.sid === sid) wb.coach = d;
    } else {
      await post('/api/summarize', {
        session_id: sid, mode: scope, engine: engine || 'auto',
      });
      const d = await api('/api/summary?session_id=' + encodeURIComponent(sid) + '&scope=' + scope);
      if (S.wb.sid === sid && d && d.ok && d.payload) wb.summaries[scope] = d.payload;
      if (S.wb.sid === sid && scope === 'content') {
        try {
          await loadSessions();
          renderWbHeader();
          renderSidebar();
        } catch (e) {}
      }
    }
    if (S.wb.sid === sid) toast(scope === 'coach' ? '复盘已更新' : '总结已生成');
  } catch (e) {
    if (S.wb.sid === sid) {
      wb.summaryError = { scope: scope, message: e.message };
      toast('生成失败：' + e.message);
    }
  }
  wb.busy = false;
  if (S.wb.sid === sid) renderWbSummary();
}

/* ================================================================ 设置（原生）
   分类 + 搜索 + 即时保存；主题是壳本地设置，其余 prefs 存引擎。 */
const FALLBACK_HINTS = {
  ask: '直取失败时会先问你。',
  auto: '按场次自动选择回退方式。',
  per_room: '每个房间单独开一个浏览器。',
  shared: '多个房间共用一个浏览器窗口。',
  none: '直取失败就停下，不自动打开浏览器。',
};
let savedTimer = null;
function markSaved() {
  const el = $('#saveState');
  if (!el) return;
  el.textContent = '已保存 · 刚刚';
  el.classList.add('is-saved');
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => {
    el.textContent = '已保存';
    el.classList.remove('is-saved');
  }, 1800);
}
function updateFallbackHint() {
  const el = $('#fallbackHint');
  const sel = $('#prefFallback');
  if (el && sel) el.textContent = FALLBACK_HINTS[sel.value] || FALLBACK_HINTS.ask;
}
function openSettings() {
  if (!S.set.loaded) loadSettings();
  else renderSettings();
}
async function loadSettings() {
  const st = S.set;
  if (st.busy) return;
  st.busy = true;
  try {
    const [cfg, lan] = await Promise.all([
      api('/api/settings'),
      api('/api/lan').catch(() => null),
    ]);
    st.cfg = cfg;
    st.prefs = cfg.prefs || {};
    st.profiles = cfg.profiles || [];
    st.current = (cfg.current && cfg.current.id) || cfg.active || '';
    st.lan = lan;
    st.loaded = true;
  } catch (e) {
    toast('设置加载失败：' + e.message);
  } finally {
    st.busy = false;
  }
  if (S.view !== 'settings') return;
  renderSettings();
  fillProfileForm();
  setSettingsPane(st.pane);
}
function fillProfileForm() {
  const st = S.set;
  const cur = st.profiles.find((p) => p.id === st.current) || st.profiles[0];
  if (!cur) return;
  $('#setBase').value = cur.base || '';
  $('#setModel').value = cur.model || '';
  const temp = cur.temperature != null ? cur.temperature : 0.3;
  $('#setTemp').value = String(temp);
  $('#setTempVal').value = String(temp);
  $('#setKey').value = '';
  const cs = $('#connState');
  cs.className = 'conn-state';
  cs.textContent = cur.has_key ? '已保存 Key · 未测试' : '未配置 Key';
}
function renderProfiles() {
  const st = S.set;
  const list = $('#profileList');
  if (!st.profiles.length) {
    list.innerHTML = '<div class="profile-empty">还没有保存连接。填下面的地址和模型，保存后会出现在这里。</div>';
    return;
  }
  list.innerHTML = st.profiles.map((p) => {
    const on = p.id === st.current;
    return '<button class="profile-item' + (on ? ' on' : '') + '" data-profile="' + esc(p.id) + '">' +
      '<span class="profile-name">' + (on ? '<i class="active-dot"></i>' : '') +
      esc(p.name || p.model || '未命名连接') + '</span>' +
      '<span class="profile-meta">' + esc((p.base || '') + ' · ' + (p.model || '')) +
      (p.configured || p.has_key ? ' · 已配置 Key' : ' · 未配置 Key') + '</span></button>';
  }).join('');
}
function renderSettings() {
  const st = S.set;
  const prefs = st.prefs || {};
  const val = (sel, v) => { const el = $(sel); if (el) el.value = v == null ? '' : String(v); };
  val('#prefFallback', prefs.direct_fallback || 'ask');
  val('#prefOpenMode', prefs.open_mode || 'internal');
  val('#prefCaptureMode', prefs.capture_mode || 'browser');
  val('#prefCloseAction', prefs.close_action || 'tray');
  val('#prefTopics', prefs.topics || 10);
  const perfMode = prefs.perf_mode || 'auto';
  $$('input[name="perfMode"]').forEach((inp) => { inp.checked = inp.value === perfMode; });
  $$('.perf-card').forEach((card) => {
    const inp = card.querySelector('input');
    card.classList.toggle('is-active', !!(inp && inp.checked));
  });
  val('#prefMaxRooms', prefs.max_rooms || 5);
  val('#prefWeeklyTime', prefs.weekly_time || 'mon-09');
  const minutes = [1, 2, 5, 10].reduce((best, n) =>
    Math.abs(n * 60 - Number(prefs.follow_interval_s || 120)) <
    Math.abs(best * 60 - Number(prefs.follow_interval_s || 120)) ? n : best, 2);
  val('#prefFollowInterval', minutes);
  const weekly = $('#prefWeekly');
  if (weekly) weekly.checked = prefs.weekly_enabled !== false;
  const auto = $('#prefAutoRecord');
  if (auto) auto.checked = prefs.auto_record_enabled === true;
  const notifyBox = $('#prefNotify');
  if (notifyBox) notifyBox.checked = prefs.notify_enabled !== false;
  const ask = $('#prefAskSuggest');
  if (ask) ask.checked = prefs.qa_suggest_enabled !== false;
  const low = $('#prefLowPower');
  if (low) low.checked = prefs.low_power_pause !== false;
  renderPerfAdvice();
  updateFallbackHint();
  const lan = st.lan || {};
  $('#prefLan').checked = !!lan.enabled;
  $('#lanDetails').hidden = !lan.enabled;
  $('#lanUrl').textContent = lan.url || '—';
  const warn = $('#lanWarn');
  if (warn) {
    const msg = lan.error || lan.firewall_error ||
      (lan.firewall === 'blocked' ? '系统防火墙可能拦截了局域网访问。' : '');
    warn.hidden = !msg;
    warn.textContent = msg || '';
  }
  renderProfiles();
  $('#aboutEngine').textContent = API || '—';
  const v = (S.health && (S.health.version || S.health.app_version)) || '0.1.0';
  $('#aboutVersion').textContent = 'v' + String(v).replace(/^v/, '');
  applyTheme();
  if (st.search) filterSettings(st.search);
}
function renderPerfAdvice() {
  const el = $('#perfAdvice');
  if (!el || !S.perf) return;
  const d = S.perf;
  const cores = Number((d.detected || {}).cores) || 0;
  const ram = Number((d.detected || {}).ram_mb) || 0;
  const names = { lite: '省电', full: '高性能', auto: '均衡' };
  const now = names[d.mode] || d.mode || '均衡';
  const advice = names[d.advice] || d.advice || '均衡';
  const power = d.power || {};
  const battery = power.supported && power.percent >= 0
    ? ' · 电量 ' + power.percent + '%' + (power.on_battery ? '（未接电源）' : '')
    : '';
  el.textContent = '当前：' + cores + ' 核 / ' +
    (ram ? Math.round(ram / 1024) + ' GB' : '内存未知') + battery +
    ' · 建议' + advice + '，实际按' + now + '运行。';
}
function renderPerfOut() {
  const out = $('#perfOut');
  if (!out || !S.perf) return;
  const e = S.perf.effective || {};
  const names = { lite: '省电', full: '高性能', auto: '均衡' };
  const yes = (v) => (v ? '开' : '关');
  const power = S.perf.power || {};
  out.hidden = false;
  out.textContent = [
    '档位：' + (names[S.perf.mode] || S.perf.mode),
    'ASR worker：' + e.asr_workers + ' · 线程：' + e.asr_threads,
    '实时部分结果：' + yes(e.partials) + ' · 实时高光卡：' + yes(e.live_highlight_card),
    '低电量暂停：' + (power.supported
      ? (S.perf.low_power ? '已触发（' + power.percent + '%）' : '待机')
      : '这台电脑没有可读的电池'),
    '通知轮询：' + e.notify_poll_s + ' 秒 · 状态轮询：' + e.live_poll_s + ' 秒',
    '笔记库每页：' + e.library_page + ' 条 · 逐字稿窗口：' + e.transcript_window + ' 句',
    '问笔记：top ' + e.qa_top_k + ' / ' + e.qa_tokens + ' tokens',
    S.perf.note || '',
  ].filter(Boolean).join('\n');
}
async function savePerfMode(value) {
  await savePref('perf_mode', value);
  await loadPerf();
  renderPerfAdvice();
  renderPerfOut();
  toast('性能档位已改为' + ({ auto: '均衡', full: '高性能', lite: '省电' }[value] || value) + '。');
}
async function savePref(key, value) {
  try {
    const d = await post('/api/settings/prefs', { [key]: value });
    S.set.prefs = Object.assign({}, S.set.prefs || {}, (d && d.prefs) || d || { [key]: value });
    if (key === 'direct_fallback') updateFallbackHint();
    if (key === 'close_action') fetchCloseAction();
    markSaved();
  } catch (e) {
    toast('保存失败：' + e.message);
  }
}
function setSettingsPane(pane) {
  S.set.pane = pane;
  $$('#setNav button').forEach((b) => {
    const on = b.dataset.setpane === pane;
    b.classList.toggle('on', on);
    if (on) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  $$('#setContent .set-pane').forEach((p) => p.classList.toggle('on', p.dataset.pane === pane));
  const content = $('#setContent');
  if (content) content.scrollTop = 0;
  const search = $('#setSearch');
  if (search) search.value = '';
  S.set.search = '';
  filterSettings('');
}
function filterSettings(q) {
  S.set.search = q || '';
  const query = String(q || '').trim().toLowerCase();
  const pane = $('#setContent .set-pane[data-pane="' + S.set.pane + '"]');
  if (!pane) return;
  let visible = 0;
  $$('.set-row', pane).forEach((row) => {
    let hay = row.textContent || '';
    $$('select', row).forEach((sel) => {
      if (sel.selectedOptions && sel.selectedOptions[0]) hay += ' ' + sel.selectedOptions[0].textContent;
    });
    $$('input', row).forEach((inp) => {
      if (inp.type !== 'password') hay += ' ' + (inp.value || '');
    });
    const show = !query || hay.toLowerCase().includes(query);
    row.hidden = !show;
    if (show) visible += 1;
  });
  $$('.set-group', pane).forEach((group) => {
    const rows = $$('.set-row', group);
    group.hidden = rows.length > 0 && rows.every((r) => r.hidden);
  });
  const none = $('#setNoMatch');
  if (none) none.hidden = !(query && visible === 0);
}
async function toggleLan(on) {
  try {
    const d = await post('/api/lan', { enabled: !!on });
    S.set.lan = d;
    renderSettings();
    markSaved();
    if (on && d.url) toast('手机访问已开启：' + d.url);
    else if (d && d.error) toast('开启失败：' + d.error);
  } catch (e) {
    const box = $('#prefLan');
    if (box) box.checked = !on;
    toast('操作失败：' + e.message);
  }
}
async function activateProfile(id) {
  try {
    await post('/api/settings/activate', { id });
    S.set.current = id;
    toast('已切换模型连接');
    await loadSettings();
  } catch (e) { toast('切换失败：' + e.message); }
}
async function saveConnection() {
  const base = $('#setBase').value.trim();
  const model = $('#setModel').value.trim();
  if (!base || !model) { toast('接口地址和模型都要填'); return; }
  const cur = S.set.profiles.find((p) => p.id === S.set.current);
  const body = {
    name: model,
    provider: (cur && cur.provider) || 'openai',
    base,
    model,
    temperature: Number($('#setTemp').value) || 0.3,
  };
  if (cur) body.id = cur.id;
  const key = $('#setKey').value.trim();
  if (key) body.key = key;
  try {
    await post('/api/settings', body);
    $('#setKey').value = '';
    toast('连接已保存');
    markSaved();
    await loadSettings();
  } catch (e) { toast('保存失败：' + e.message); }
}
async function testConnection() {
  const st = $('#connState');
  st.className = 'conn-state busy';
  st.textContent = '测试中…';
  try {
    const d = await post('/api/llm/test', {
      base: $('#setBase').value.trim(),
      model: $('#setModel').value.trim(),
      key: $('#setKey').value.trim(),
    });
    st.className = 'conn-state ' + (d.ok ? 'ok' : 'bad');
    st.textContent = d.ok ? '连接正常 · ' + (d.ms || 0) + 'ms' : ('失败：' + (d.error || '未知错误'));
  } catch (e) {
    st.className = 'conn-state bad';
    st.textContent = '失败：' + e.message;
  }
}

/* ================================================================ 记录 HUD */
const HUD_STATE = {
  starting: { label: '正在打开直播间…', tone: 'starting' },
  recording: { label: '正在记录', tone: '' },
  paused: { label: '已暂停', tone: 'paused' },
  quiet: { label: '正在记录', tone: 'quiet' },
  stalled: { label: '听不到声音了', tone: 'stalled' },
  silent: { label: '直播可能静音了', tone: 'silent' },
  error: { label: '录音可能中断了', tone: 'error' },
  stopped: { label: '记录已结束', tone: 'stopped' },
};
function fmtHudTimer(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return pad(h) + ':' + pad(m) + ':' + pad(s);
}
function hudSpeakers() {
  return (S.speakersSid && S.speakersSid === S.hudSid)
    ? (S.speakers || []).map(normSpeaker).filter((x) => x.id)
    : [];
}
function hudStateOf(room, s) {
  if (!room) return 'idle';
  const st = stateInfo(room);
  if (st.k === 'failed') return 'error';
  if (st.k === 'stopped') return 'stopped';
  if (st.k === 'pending') return 'starting';
  if (s && s.paused) return 'paused';
  if (s && s.last_chunk_ago_s != null && Number(s.last_chunk_ago_s) > 30) return 'stalled';
  if (s && Number(s.zero_s || 0) >= 120) return 'silent';
  if (s && Number(s.idle_s || 0) > 30) return 'quiet';
  return 'recording';
}
function hudTitle(room, s) {
  if (s && s.title && s.title !== roomName(room)) return s.title;
  return roomName(room);
}

function hudHighlight() {
  if (perfLite() || (S.perf && S.perf.low_power) ||
      !S.sessInfo || S.sessInfo.id !== S.hudSid) return null;
  if (S.sessInfo.paused || Date.now() < S.hudHi.dismissUntil) return null;
  const list = Array.isArray(S.sessInfo.live_highlights) ? S.sessInfo.live_highlights : [];
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const h = list[i];
    if (h && h.id && !S.hudHi.saved.has(h.id)) return h;
  }
  return null;
}
function renderHudHighlight() {
  const el = $('#hudHighlight');
  if (!el) return;
  const h = hudHighlight();
  if (!h) {
    if (!el.hidden) el.innerHTML = '';
    el.hidden = true;
    S.hudHi.current = '';
    return;
  }
  const changed = S.hudHi.current !== h.id;
  S.hudHi.current = h.id;
  el.hidden = false;
  el.dataset.hhId = h.id;
  if (!changed) return;
  el.innerHTML =
    '<div class="hh-head"><span>这段话可能值得回听</span><b>' +
    esc(fmtDur(h.t0)) + '–' + esc(fmtDur(h.t1)) + '</b></div>' +
    (h.quote ? '<p class="hh-quote">「' + esc(h.quote) + '」</p>' : '') +
    '<div class="hh-actions"><button type="button" data-hh="play">试听</button>' +
    '<button type="button" data-hh="save">收藏</button>' +
    '<button type="button" data-hh="skip">不用</button></div>';
}

function renderHud() {
  const room = hudRoom();
  const s = sessionFor(room) || (S.sessInfo && S.sessInfo.id === S.hudSid ? S.sessInfo : null);
  const hasFinish = !!S.finish;
  $('#finishCard').hidden = !hasFinish;
  $('#hudCard').hidden = hasFinish || !room;
  $('#hudIdle').hidden = hasFinish || !!room;

  if (hasFinish) {
    $('#hudHighlight').hidden = true;
    renderFinish();
    return;
  }
  if (!room) {
    S.hudClock = { elapsed: 0, at: 0, paused: false, sid: '' };
    $('#hudHighlight').hidden = true;
    return;
  }

  const sid = room.session_id || '';
  if (sid && sid !== S.hudSid) {
    S.hudSid = sid;
    S.hudHi = { saved: new Set(), dismissUntil: 0, current: '' };
  }
  const st = stateInfo(room);
  // 看门狗/采集页把这一路停了：不把用户丢回空态，直接进入收尾进度。
  if (st.k === 'stopped' && sid && !S.finish) {
    const age = Date.now() - (Date.parse(room.updated_at || '') || 0);
    if (age >= 0 && age < 30 * 60 * 1000) {
      startFinishWatch(room, true);
      return;
    }
  }

  const k = hudStateOf(room, s);
  const info = HUD_STATE[k] || HUD_STATE.recording;
  const card = $('#hudCard');
  card.dataset.state = info.tone || k;
  $('#hudState').innerHTML = '<i></i><b>' + esc(info.label) + '</b>';
  $('#hudRoom').textContent = hudTitle(room, s);

  const elapsed = Number(s ? s.elapsed : 0) || 0;
  S.hudClock = { elapsed, at: performance.now(), paused: !!((s || {}).paused), sid };
  $('#hudTimer').textContent = fmtHudTimer(elapsed);

  const people = hudSpeakers().length;
  const utts = Number((s && s.n_utterances) || 0);
  const bits = [];
  if (k === 'starting') {
    bits.push('正在连接声音…');
  } else {
    if (people) bits.push(people + ' 位说话人');
    bits.push('已转写 ' + utts + ' 句');
  }
  $('#hudStats').textContent = bits.join(' · ');

  const sPaused = !!(s && s.paused);
  $('#hudPause').textContent = sPaused ? '继续' : '暂停';
  $('#hudPause').disabled = !sid || st.k !== 'live';
  $('#hudMark').disabled = !sid || st.k === 'pending';
  $('#hudFinish').disabled = !sid;

  // 最近字幕：只留定稿，末句临时结果单独一行。
  const rows = (S.lines || []).filter((r) => r && r.text).slice(-8);
  $('#hudEmpty').hidden = rows.length > 0 || !!S.partial;
  $('#hudLines').innerHTML = rows.map((r) => {
    const t = r.t_start != null ? fmtDur(r.t_start) : (r.t != null ? fmtDur(r.t) : '');
    const label = r.speaker_label || r.speaker || '说话人';
    return '<div class="hud-line"><i style="background:' + spkColor(r.speaker_id || label) + '"></i>' +
      '<span class="who">' + esc(label) + '</span>' +
      '<span class="say">' + esc(r.text) +
      (t ? '<span class="at">' + esc(t) + '</span>' : '') + '</span></div>';
  }).join('');
  const part = S.partial && (S.partial.text || S.partial.transcript || '');
  $('#hudPartial').hidden = !part;
  $('#hudPartial').textContent = part ? ('识别中… ' + part) : '';

  renderHudHighlight();

  const marks = (s && Array.isArray(s.marks)) ? s.marks : [];
  $('#hudMarks').hidden = marks.length === 0;
  if (marks.length) {
    $('#hudMarks').innerHTML = '<span class="hm-label">已标记 ' + marks.length + ' 处</span>' +
      marks.slice(-3).map((m) =>
        '<button data-hud-mark="' + Number(m.t || 0) + '" title="从这一秒开始回听">' +
        esc(fmtDur(m.t)) + '</button>').join('');
  }

  const multi = liveRooms().length;
  $('#hudMulti').hidden = multi <= 1;
  $('#hudMulti').textContent = '还有 ' + Math.max(0, multi - 1) + ' 个直播间在记录 · 切换到多路';

  const alert = $('#hudAlert');
  let alertHtml = '';
  if (k === 'starting') {
    alertHtml = '<b>正在连接声音…</b><span>第一次打开直播页会多等几秒。如果一直停在这里，可以去收听页看看采集状态。</span>' +
      '<span class="grow"></span><button data-hud-act="listen">打开收听</button>';
  } else if (k === 'stalled') {
    alertHtml = '<b>听不到声音了</b><span>已经 ' + Math.round(Number(s.last_chunk_ago_s) || 0) +
      ' 秒没有收到声音，可能浏览器关了或网络断了。录音里这一段是空的。</span>' +
      '<span class="grow"></span>' +
      '<button data-hud-act="retry">重新连接</button>' +
      '<button data-hud-act="finish">结束并保存</button>';
  } else if (k === 'silent') {
    alertHtml = '<b>连续没有声音</b><span>已经 ' + Math.max(1, Math.round(Number(s.zero_s) / 60)) +
      ' 分钟没有声音。直播可能已经结束，看门狗会在更久没声音后自动收尾。</span>' +
      '<span class="grow"></span><button data-hud-act="live">看画面</button>' +
      '<button data-hud-act="finish">结束并保存</button>';
  } else if (k === 'quiet') {
    alertHtml = '<b>已经 ' + Math.floor(Number(s.idle_s) || 0) +
      ' 秒没听到人说话</b><span>直播还开着吗？如果只是安静，可以继续记录。</span>' +
      '<span class="grow"></span><button data-hud-act="live">看画面</button>';
  } else if (k === 'error') {
    alertHtml = '<b>这一路没有在采集</b><span>' +
      esc(String(room.error || '采集链路断了。').slice(0, 120)) +
      '</span><span class="grow"></span>' +
      '<button data-hud-act="retry">重试</button>' +
      '<button data-hud-act="finish">结束并保存</button>';
  }
  alert.hidden = !alertHtml;
  alert.className = 'hud-alert' + (k === 'quiet' ? ' calm' : '');
  alert.innerHTML = alertHtml;
}

function hudTick() {
  if (S.view !== 'hud' || S.finish) return;
  const c = S.hudClock;
  if (!c || !c.sid || !c.at) return;
  const extra = c.paused ? 0 : Math.max(0, (performance.now() - c.at) / 1000);
  $('#hudTimer').textContent = fmtHudTimer(c.elapsed + extra);
}

function confirmFinish(room) {
  if (!room || !room.session_id) { toast('这一路还没有可保存的内容。'); return; }
  const s = sessionFor(room);
  const short = s && Number(s.elapsed) < 60;
  openModal(
    '<h3>结束这场记录？</h3>' +
    '<p>结束后会自动整理成笔记。录音和逐字稿会先保存好，不会丢。' +
    (short ? '<br><span style="color:var(--amber)">这场比较短，可能生成不了要点。</span>' : '') +
    '</p>' +
    '<div class="sheet-actions"><button class="btn" data-mkeep>继续记录</button>' +
    '<button class="btn primary" data-mfinish>结束并生成笔记</button></div>',
    (sheet) => {
      const keep = sheet.querySelector('[data-mkeep]');
      // 默认焦点给「继续记录」：长直播里误按 Enter 不该结束。
      setTimeout(() => { try { keep.focus(); } catch (e) {} }, 30);
      keep.onclick = closeModal;
      sheet.querySelector('[data-mfinish]').onclick = () => {
        closeModal();
        startFinish(room);
      };
    }
  );
}

function startFinish(room, alreadyStopped) {
  const sid = room && room.session_id;
  if (!sid) { toast('这一路还没有可保存的内容。'); return; }
  if (S.finish) return;
  S.hudRoomId = room.id;
  S.hudSid = sid;
  S.finish = {
    sid, roomId: room.id, saveDone: !!alreadyStopped, step: alreadyStopped ? 'tidy' : 'save',
    started: Date.now(), job: null, jobId: '', failed: '', error: '', done: false,
    polling: false, autoOpened: false,
  };
  renderHud();
  if (!alreadyStopped) {
    post('/api/rooms/close', { id: room.id })
      .then(() => {
        if (!S.finish || S.finish.sid !== sid) return;
        S.finish.saveDone = true;
        S.finish.step = 'tidy';
        renderFinish();
        pollFinish();
      })
      .catch((e) => {
        if (!S.finish || S.finish.sid !== sid) return;
        // 房间可能已经被看门狗停了；确认一下再决定是不是「保存失败」。
        const r = S.rooms.find((x) => x.id === room.id);
        if (r && stateInfo(r).k === 'stopped') {
          S.finish.saveDone = true;
          S.finish.step = 'tidy';
        } else {
          S.finish.failed = 'save';
          S.finish.error = '保存录音时出了点问题：' + (e.message || '请稍后重试');
        }
        renderFinish();
        pollFinish();
      });
  } else {
    pollFinish();
  }
  refreshRooms();
}

function startFinishWatch(room, alreadyStopped) {
  if (S.finish) return;
  startFinish(room, alreadyStopped);
}

function finishStepState(f, key) {
  if (f.failed === 'save' && key === 'save') return 'fail';
  if (f.failed && key !== 'save') {
    if (key === 'note') return 'fail';
    if (key === 'tidy') return 'done';
  }
  if (key === 'save') return f.saveDone ? 'done' : 'on';
  if (key === 'tidy') {
    if (!f.saveDone) return '';
    if (f.done) return 'done';
    return 'on';
  }
  if (key === 'note') {
    if (f.done) return 'done';
    if (f.failed) return 'fail';
    if (f.job && f.job.status === 'running') return 'on';
    if (f.job && f.job.status === 'queued') return '';
    return f.saveDone ? '' : '';
  }
  return '';
}
function renderFinish() {
  const f = S.finish;
  if (!f) { $('#finishCard').hidden = true; renderHud(); return; }
  $('#finishCard').hidden = false;
  $('#hudCard').hidden = true;
  $('#hudIdle').hidden = true;

  const done = f.done;
  const failed = !!f.failed;
  $('#fcTitle').textContent = done ? '笔记已生成' : (failed ? '笔记没生成成功' : '正在生成笔记');
  $('#fcSub').textContent = done
    ? '录音、逐字稿和这次生成的笔记都已经保存好。'
    : (failed
      ? '录音和逐字稿都在。可以重试生成，或先看原文。'
      : '录音和逐字稿会先保存好，可以关掉窗口去做别的。');
  const statusText = {
    save: ['正在保存', '已保存', '没保存成功'],
    tidy: ['正在整理', '已整理', ''],
    note: ['正在生成', '已生成', '生成失败'],
  };
  $$('#fcSteps li').forEach((li) => {
    const key = li.dataset.fc;
    const state = finishStepState(f, key);
    li.className = state;
    const span = li.querySelector('span');
    const texts = statusText[key] || ['', '', ''];
    span.textContent = state === 'done' ? texts[1]
      : (state === 'fail' ? texts[2] : (state === 'on' ? texts[0] : '等待中'));
  });
  const note = $('#fcNote');
  note.hidden = !f.error;
  note.textContent = f.error || '';
  $('#fcOpen').hidden = !done;
  $('#fcRetry').hidden = !(failed && f.saveDone);
  $('#fcRaw').hidden = !(failed && f.saveDone);
  $('#fcLater').hidden = !(done || failed);
  // 完成后「打开笔记」是唯一主操作；失败时把「重试生成」顶上来。
  // 「再记一场 / 先留在记录卡」只是退出收尾卡，不该抢实心按钮。
  $('#fcOpen').classList.toggle('primary', done);
  $('#fcRetry').classList.toggle('primary', !!failed && f.saveDone);
  $('#fcLater').textContent = done ? '再记一场' : '稍后再看';
}

async function pollFinish() {
  const f = S.finish;
  if (!f || f.polling) return;
  f.polling = true;
  const tick = async () => {
    if (!S.finish || S.finish.sid !== f.sid) return;
    try {
      const d = await api('/api/jobs?session_id=' + encodeURIComponent(f.sid) + '&limit=30');
      const jobs = Array.isArray(d.jobs) ? d.jobs : [];
      const job = jobs.find((j) => j.kind === 'summary') || null;
      if (job && job.id !== f.jobId) f.jobId = job.id;
      f.job = job;
      if (job && job.status === 'succeeded') {
        f.done = true;
        f.failed = '';
        f.error = '';
      } else if (job && job.status === 'failed') {
        f.failed = 'note';
        f.error = '笔记没有生成成功：' + String(job.error || '模型或任务出了点问题').slice(0, 160);
      }
      renderFinish();
      if (f.done || f.failed) {
        f.polling = false;
        if (f.done && !f.autoOpened && document.hasFocus()) {
          f.autoOpened = true;
          setTimeout(() => {
            if (S.finish && S.finish.sid === f.sid && S.view === 'hud') openNoteFor(f.sid);
          }, 1600);
        }
        return;
      }
      if (f.saveDone && !job && Date.now() - f.started > 120000) {
        f.failed = 'note';
        f.error = '笔记任务迟迟没有开始，可以重试。录音和逐字稿都在。';
        f.polling = false;
        renderFinish();
        return;
      }
      if (Date.now() - f.started > 15 * 60 * 1000) {
        f.failed = 'note';
        f.error = '生成笔记花的时间有点长，可以稍后重试。录音和逐字稿都在。';
        f.polling = false;
        renderFinish();
        return;
      }
    } catch (e) {
      /* 引擎刚重启/网络抖一下：下一拍再试 */
    }
    setTimeout(tick, 2000);
  };
  setTimeout(tick, 1200);
}

function openNoteFor(sid, t) {
  if (!sid) { switchView('library'); return; }
  S.selectedSession = sid;
  switchView('note', { session: sid, t: t, back: 'library' });
}

function dismissFinish() {
  S.finish = null;
  S.finishTimer = null;
  // 不要留住刚停下的房间：hudRoom() 会把它当成「刚被看门狗停掉」，
  // 立刻又触发一次收尾卡。清掉房间焦点，回到「还没有在记录」的空态。
  S.hudRoomId = '';
  S.hudSid = '';
  S.hudClock = { elapsed: 0, at: 0, paused: false, sid: '' };
  S.lines = [];
  S.speakers = [];
  S.speakersSid = '';
  S.partial = null;
  renderHud();
  refreshRooms();
}

/* ================================================================ room ops */
async function selectRoom(id) {
  if (!id || id === S.selectedId) {
    if (id && S.stage === 'wall') setStage('signal');
    return;
  }
  const keepAnnot = S.stage === 'annotate';
  closeSpeakerPop();
  if (S.pageOpen) await closeStageView();
  S.selectedId = id;
  S.selectedSid = (selectedRoom() || {}).session_id || '';
  S.lines = [];
  S.speakers = [];
  S.speakersSid = '';
  S.partial = null;
  S.sessInfo = null;
  S.annotSkipped = new Set();
  S.stage = keepAnnot ? 'annotate' : 'signal';
  renderSidebar();
  renderStage();
  renderInspector();
  pollTranscript();
  if (keepAnnot) ensureAnnotPage();
}

async function addRoom() {
  const input = $('#addInput');
  const url = (input.value || '').trim();
  if (!url || S.busy) return;
  S.busy = true;
  $('#addGo').disabled = true;
  $('#addGo').textContent = '添加中…';
  try {
    const r = await post('/api/rooms', { url });
    input.value = '';
    if (r.created === false && r.room) {
      toast('这个链接已经在列表里了，已帮你选中。');
      S.selectedId = r.room.id;
    } else {
      toast('已加入收听，正在启动采集。');
    }
    S.addOpen = false;
    $('#addBox').classList.remove('on');
    await refreshRooms();
    if (r.room && r.room.id) {
      S.selectedId = r.room.id;
      renderSidebar(); renderStage(); renderInspector();
    }
    // 只有一路时直接进记录卡；多路仍留在收听，避免 HUD 里还要先选人。
    const active = S.rooms.filter((x) => ['pending', 'live'].includes(stateInfo(x).k));
    if (active.length === 1) {
      focusHudRoom(active[0]);
      switchView('hud', { force: true, room: active[0].id });
    }
  } catch (e) {
    toast('加不进去：' + e.message);
  } finally {
    S.busy = false;
    $('#addGo').disabled = false;
    $('#addGo').textContent = '开始听';
  }
}

async function stopRoom(room) {
  if (!room) return;
  try {
    await post('/api/rooms/close', { id: room.id });
    toast('已停止，录音会正常收尾保存。');
    if (S.pageRoom === room.id) await closeStageView();
    await refreshRooms();
  } catch (e) {
    toast('停止失败：' + e.message);
  }
}

async function retryRoom(room) {
  if (!room) return;
  try {
    await post('/api/rooms/state', { id: room.id, state: 'pending', error: '' });
    toast('已重新排队，采集端会重新拉起这一路。');
    await refreshRooms();
  } catch (e) {
    toast('重试失败：' + e.message);
  }
}

/* ================================================================ 对象菜单与数据闭环 */
function findSession(id) {
  return S.sessions.find((s) => s.id === id) || null;
}
function findRoom(id) {
  return S.rooms.find((r) => r.id === id) || null;
}

function openWorkbenchPlay(id) {
  S.selectedSession = id;
  switchView('workbench', { session: id, autoplay: true });
}

function sessionMenu(id, x, y) {
  const s = findSession(id);
  if (!s) return;
  const canPlay = s.audio_state === 'ready';
  showCtxMenu([
    { icon: 'open', label: '打开工作台', hint: '↵', run: () => {
      S.selectedSession = id;
      switchView('workbench', { session: id });
    } },
    { icon: 'play', label: '从开头播放', disabled: !canPlay, run: () => openWorkbenchPlay(id) },
    { sep: true },
    { icon: 'rename', label: '重命名', run: () => renameSessionDialog(id) },
    { icon: 'copy', label: '复制场次 ID', run: () => copyText(id) },
    { icon: 'export', label: '导出 Markdown', run: () => downloadSession(id) },
    { sep: true },
    { icon: 'trash', label: '移到回收站…', danger: true, run: () => confirmTrashSession(id) },
  ], x, y);
}

function roomMenu(id, x, y) {
  const r = findRoom(id);
  if (!r) return;
  const st = stateInfo(r);
  showCtxMenu([
    // 多路时 HUD 不能从导航直接进（见 switchView 的多路分支）；这条是「选一路」之后的入口。
    { icon: 'record', label: '进入记录卡', hint: '暂停 / 标记 / 结束',
      disabled: !canEnterHud(r), run: () => openHudFor(r) },
    { icon: 'play', label: '打开实时字幕', disabled: !r.session_id, run: () => {
      switchView('live', { session: r.session_id, back: 'listen' });
    } },
    { icon: 'page', label: '显示采集页面', disabled: !(r.url || r.resolved_url), run: () => {
      selectRoom(r.id).then(() => openStagePage());
    } },
    { icon: 'link', label: '复制直播链接', disabled: !(r.url || r.resolved_url),
      run: () => copyText(r.resolved_url || r.url) },
    { sep: true },
    { icon: 'retry', label: '重试采集', disabled: st.k === 'live' || st.k === 'pending',
      run: () => retryRoom(r) },
    { icon: 'stop', label: '停止采集', disabled: st.k === 'stopped', run: () => confirmStopRoom(r) },
    { sep: true },
    { icon: 'remove', label: '移除这一路…', danger: true, run: () => confirmRemoveRoom(r) },
  ], x, y);
}

function renameSessionDialog(id) {
  const s = findSession(id) || {};
  openModal(
    '<h3>重命名场次</h3>' +
    '<p>改名后，自动总结不会再覆盖你起的标题。</p>' +
    '<input class="modal-input" id="renameInput" value="' + esc(s.title || '') + '" maxlength="60">' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn primary" data-mconfirm="rename">保存</button></div>',
    (sheet) => {
      const input = sheet.querySelector('#renameInput');
      const ok = async () => {
        const title = input.value.trim();
        if (!title) { toast('标题不能为空'); return; }
        try {
          await post('/api/sessions/rename', { session_id: id, title });
          closeModal();
          toast('已重命名');
          await loadSessions();
          if (S.view === 'workbench' && S.selectedSession === id) setWorkbenchFrame(id);
        } catch (e) { toast('重命名失败：' + e.message); }
      };
      input.focus();
      input.select();
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); ok(); }
      });
      sheet.querySelector('[data-mconfirm="rename"]').onclick = ok;
    }
  );
}

async function downloadSession(id) {
  try {
    const res = await fetch(API + '/api/export?session_id=' + encodeURIComponent(id) + '&format=md');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = id + '.md';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast('已导出 Markdown');
  } catch (e) {
    toast('导出失败：' + e.message);
  }
}

function confirmTrashSession(id) {
  const s = findSession(id) || {};
  openModal(
    '<h3>移到回收站？</h3>' +
    '<p>「' + esc(s.title || '未命名场次') + '」的音频会一起移入回收站，逐字稿和总结保留。' +
    '可以在文件中心还原。</p>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn danger" data-mconfirm="trash">移到回收站</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mconfirm="trash"]').onclick = async () => {
        closeModal();
        try {
          await post('/api/library/trash', { session_id: id });
          S.sessions = S.sessions.filter((x) => x.id !== id);
          if (S.selectedSession === id) {
            const next = S.sessions.find((x) => x.audio_state === 'ready' &&
              Number(x.n_utterances) > 0) || S.sessions.find((x) => Number(x.n_utterances) > 0)
              || S.sessions[0];
            S.selectedSession = next ? next.id : '';
            if (S.view === 'workbench') setWorkbenchFrame(S.selectedSession);
          }
          renderSidebar();
          renderStage();
          renderInspector();
          toast('已移到回收站，可在文件中心还原');
        } catch (e) {
          toast('删除失败：' + e.message);
        }
      };
    }
  );
}

function confirmStopRoom(room) {
  if (!room) return;
  openModal(
    '<h3>停止「' + esc(roomName(room)) + '」？</h3>' +
    '<p>会停止采集并关闭这一路的采集浏览器。已经录下的内容会正常收尾保存，回放不受影响。</p>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn danger" data-mconfirm="stop">停止这一路</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mconfirm="stop"]').onclick = () => { closeModal(); stopRoom(room); };
    }
  );
}

function confirmRemoveRoom(room) {
  if (!room) return;
  const active = stateInfo(room).k !== 'stopped';
  openModal(
    '<h3>移除「' + esc(roomName(room)) + '」？</h3>' +
    '<p>' + (active ? '会先停止这一路采集，再' : '') +
    '从在听列表移除。已经录下的内容会保留，之后还能在工作台找到。</p>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn danger" data-mconfirm="remove">移除这一路</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mconfirm="remove"]').onclick = async () => {
        closeModal();
        try {
          await post('/api/rooms/remove', { id: room.id });
          if (S.pageRoom === room.id) await closeStageView();
          toast('已移除，已录内容保留在工作台');
          await refreshRooms();
          await loadSessions();
        } catch (e) {
          toast('移除失败：' + e.message);
        }
      };
    }
  );
}

async function clearStoppedRooms() {
  const rooms = S.rooms.filter((r) => stateInfo(r).k === 'stopped');
  if (!rooms.length) { toast('现在没有已停止的直播间。'); return; }
  openModal(
    '<h3>清除 ' + rooms.length + ' 路已停止的直播间？</h3>' +
    '<p>只从在听列表移除，不删除已录内容。每路对应的场次仍能在工作台找到。</p>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn danger" data-mconfirm="clear">清除已停止</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mconfirm="clear"]').onclick = async () => {
        closeModal();
        let done = 0;
        for (const r of rooms) {
          try { await post('/api/rooms/remove', { id: r.id }); done += 1; } catch (e) {}
        }
        toast(done ? ('已移除 ' + done + ' 路，已录内容保留') : '移除失败，请稍后再试');
        await refreshRooms();
        await loadSessions();
      };
    }
  );
}

/* ---------------- 工作台批量操作 ---------------- */
function batchIds() {
  return Array.from(S.batchSel).filter((id) => findSession(id));
}
function confirmBatchTrash() {
  const ids = batchIds();
  if (!ids.length) return;
  openModal(
    '<h3>把选中的 ' + ids.length + ' 场移到回收站？</h3>' +
    '<p>音频会一起移走，逐字稿和总结保留；可以在文件中心的回收站还原。</p>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn danger" data-mconfirm="batch-trash">移到回收站</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mconfirm="batch-trash"]').onclick = async () => {
        closeModal();
        try {
          const r = await post('/api/library/batch',
            { action: 'trash', session_ids: ids });
          const skipped = (r.skipped || []).length;
          S.batchMode = false;
          S.batchSel.clear();
          await loadSessions();
          renderSidebar();
          toast('已移入回收站 ' + (r.done || []).length + ' 场' +
            (skipped ? ('，跳过 ' + skipped + ' 场（正在录制）') : '') +
            '，可在文件中心还原');
        } catch (e) { toast('删除失败：' + e.message); }
      };
    }
  );
}
async function batchExport() {
  const ids = batchIds();
  if (!ids.length) return;
  toast('正在打包 ' + ids.length + ' 场…');
  try {
    const res = await fetch(API + '/api/export/batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_ids: ids, format: 'md' }),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { const d = await res.json(); msg = d.error || msg; } catch (e) {}
      throw new Error(msg);
    }
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'livenotes-export-' + new Date().toISOString().slice(0, 10) + '.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast('已导出 ' + ids.length + ' 场的 Markdown 压缩包');
  } catch (e) { toast('批量导出失败：' + e.message); }
}

async function loadDemo() {
  try {
    const d = await post('/api/demo/seed', {});
    S.selectedSession = d.session_id || '';
    await loadSessions();
    switchView('workbench', { session: S.selectedSession });
    toast('已加载演示数据，可右键删除');
  } catch (e) {
    toast('加载演示数据失败：' + e.message);
  }
}

function stopAll() {
  const rooms = S.rooms.filter((r) => stateInfo(r).k !== 'stopped');
  if (!rooms.length) { toast('现在没有需要停止的直播间。'); return; }
  openModal(
    '<h3>停止全部 ' + rooms.length + ' 路？</h3>' +
    '<p>会停止采集并关闭每一路的采集浏览器。已经录下的内容会正常收尾保存，之后还能在文件中心回放。</p>' +
    '<ul class="modal-list">' + rooms.slice(0, 8).map((r) =>
      '<li><span class="ch">' + roomCh(r) + '</span><span>' + esc(roomName(r)) + '</span></li>').join('') +
    (rooms.length > 8 ? '<li>… 还有 ' + (rooms.length - 8) + ' 路</li>' : '') + '</ul>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn danger" data-mconfirm="stopall">停止全部</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mconfirm="stopall"]').onclick = async () => {
        closeModal();
        for (const r of rooms) {
          try { await post('/api/rooms/close', { id: r.id }); } catch (e) {}
        }
        if (S.pageOpen) await closeStageView();
        toast('已停止全部采集。');
        await refreshRooms();
      };
    }
  );
}

/* ================================================================ close / quit */
// 关闭窗口和退出应用是两件事：
//   - 没有录音时，按设置「最小化到托盘 / 退出程序」；
//   - 有录音时一律先问：托盘继续 / 停止并退出 / 取消。
// 真正退出走 shell_quit，Tauri Exit 事件会回收引擎；Job Object 再兜底。
function activeRooms() {
  return S.rooms.filter((r) => ['live', 'pending'].includes(stateInfo(r).k));
}

async function fetchCloseAction() {
  try {
    const r = await fetch(API + '/api/settings', { cache: 'no-store' });
    if (r.ok) {
      const s = await r.json();
      const v = s && s.prefs && s.prefs.close_action;
      if (v === 'quit' || v === 'tray') S.closeAction = v;
    }
  } catch (e) {}
  return S.closeAction || 'tray';
}

function hideToTray() {
  const first = !localStorage.getItem('ln.trayHint');
  if (first) {
    localStorage.setItem('ln.trayHint', '1');
    // 首次收起：由引擎记一条终身一次的通知，托盘被收进 ^ 也能在通知中心回看。
    post('/api/notifications/tray_hint', {}).then(() => {
      pollNotifications();
    }).catch(() => {
      toast('已最小化到托盘，录音继续。右键托盘图标可以暂停或结束。');
    });
    setTimeout(() => { invoke('shell_hide').catch(() => {}); }, 3200);
    return;
  }
  invoke('shell_hide').catch(() => {});
}

async function quitNow() {
  try {
    await invoke('shell_quit');
  } catch (e) {
    toast('退出失败：' + e.message);
  }
}

async function stopRoomsAndQuit(rooms) {
  toast('正在停止 ' + rooms.length + ' 路…');
  for (const r of rooms) {
    try { await post('/api/rooms/close', { id: r.id }); } catch (e) {}
  }
  await quitNow();
}

function openQuitModal(n) {
  openModal(
    '<h3>退出 LiveNotes？</h3>' +
    '<p>还有 ' + n + ' 路在录音。退出会停止采集并结束引擎；已经录下的内容会正常收尾保存。</p>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn" data-mact="tray">最小化到托盘</button>' +
    '<button class="btn danger" data-mact="quit">停止并退出</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mact="tray"]').onclick = () => { closeModal(); hideToTray(); };
      sheet.querySelector('[data-mact="quit"]').onclick = () => {
        closeModal();
        const rooms = activeRooms();
        if (rooms.length) stopRoomsAndQuit(rooms);
        else quitNow();
      };
    }
  );
}

async function requestClose() {
  // 记录中 / 收尾中关窗一律收进托盘，不弹询问：关闭窗口不是结束记录。
  // 想真正退出的人走托盘「退出 LiveNotes」，那里才有停止/退出确认。
  if (S.finish || activeRooms().length) {
    hideToTray();
    return;
  }
  const action = await fetchCloseAction();
  if (action === 'quit') return quitNow();
  hideToTray();
}

function requestQuit(n) {
  const rooms = activeRooms();
  const count = n || rooms.length;
  if (count > 0) {
    openQuitModal(count);
    return;
  }
  quitNow();
}

/* ================================================================ stage page */
function stageRect() {
  // 标注模式里原生页面只占左栏；整块舞台留给字幕，子视图不能盖 HTML。
  const el = (S.stage === 'annotate' && $('#annotPage')) ? $('#annotPage') : $('#stageWrap');
  const r = el.getBoundingClientRect();
  return { x: Math.round(r.left), y: Math.round(r.top), w: Math.max(1, Math.round(r.width)), h: Math.max(1, Math.round(r.height)) };
}
/* 旧直取把 reflow 的 room id 当 web_rid，拼过 live.douyin.com/<id>；抖音对
   那个地址只回「服务器开小差了」。同一个 id 同时出现在 reflow 路径里，就当
   这个 resolved_url 不可信，退回 reflow 页；采集端回报真实 web_rid 之后两者
   不同，才用真实地址。 */
function roomPageUrl(room) {
  const resolved = String(room.resolved_url || '');
  const reflow = /\/reflow\/(\d+)/.exec(room.url || '');
  const live = /^https?:\/\/live\.douyin\.com\/(\d+)/.exec(resolved);
  if (live && reflow && live[1] === reflow[1]) return room.url || resolved;
  return resolved || room.url || '';
}

async function openStagePage() {
  const room = selectedRoom();
  if (!room) return;
  const url = roomPageUrl(room);
  if (!url) { toast('这一路没有可打开的页面。'); return; }
  await closeStageView();
  const label = 'stage-' + String(room.id).replace(/[^a-zA-Z0-9_-]/g, '-').slice(0, 48);
  const r = stageRect();
  try {
    await invoke('room_open', {
      label, url, sid: room.session_id || '', capture: false,
      x: r.x, y: r.y, w: r.w, h: r.h,
    });
    S.stageLabel = label;
    S.pageOpen = true;
    S.pageRoom = room.id;
    S.pageHidden = false;
    renderStage();
  } catch (e) {
    toast('页面打开失败：' + String(e));
  }
}
async function closeStageView() {
  const label = S.stageLabel;
  S.stageLabel = null;
  S.pageOpen = false;
  S.pageRoom = null;
  S.pageHidden = false;
  if (label) { try { await invoke('room_close', { label }); } catch (e) {} }
  renderStage();
}

/* 三种舞台模式：信号 / 页面 / 标注。页面与标注都要开原生子视图，
   区别只在矩形：页面占满舞台，标注只占左栏，右栏留给字幕。 */
async function setStage(mode) {
  if (!['signal', 'page', 'annotate', 'wall'].includes(mode) || S.stage === mode) return;
  const room = selectedRoom();
  S.stage = mode;
  closeSpeakerPop();
  if (mode === 'signal' || mode === 'wall') {
    if (S.pageOpen) await closeStageView();
  }
  renderStage();
  renderInspector();
  renderStatus();
  if (mode === 'page') {
    if (!room) return;
    if (!(room.url || room.resolved_url)) {
      toast('这一路没有可打开的页面。');
      S.stage = 'signal';
      renderStage();
      renderInspector();
      return;
    }
    await openStagePage();
  } else if (mode === 'annotate') {
    await ensureAnnotPage();
    pollTranscript();
  }
}

async function ensureAnnotPage() {
  if (S.stage !== 'annotate') return;
  const room = selectedRoom();
  if (!room) return;
  const url = roomPageUrl(room);
  // 登录页 / 已结束的房间不自动开页：开了也只是空白，反而挡住“没有页面”的说明。
  if (!url || stateInfo(room).k !== 'live') {
    if (S.pageOpen) await closeStageView();
    renderStage();
    return;
  }
  // 画面面板关着、或字幕正放大时不开原生视图：开了也看不见，还白占一个 WebView。
  if (!S.panes.live || S.annotMax === 'tx') {
    renderStage();
    return;
  }
  if (S.pageOpen && S.pageRoom === room.id) {
    await layoutStageView();
    renderStage();
    return;
  }
  await openStagePage();
}
async function layoutStageView() {
  if (!S.stageLabel || S.pageHidden) return;
  const r = stageRect();
  try { await invoke('room_layout', { label: S.stageLabel, x: r.x, y: r.y, w: r.w, h: r.h }); } catch (e) {}
}
let layoutTimer = 0;
function scheduleStageLayout() {
  if (!S.stageLabel) return;
  clearTimeout(layoutTimer);
  layoutTimer = setTimeout(layoutStageView, 120);
}
async function hideStageForOverlay() {
  await syncStageVisible();
}
async function restoreStageFromOverlay() {
  await syncStageVisible();
}

/* ================================================================ overlay */
function openModal(html, mount) {
  if (S.modalOpen) return;
  S.overlayDepth++;
  if (S.overlayDepth === 1) hideStageForOverlay();
  S.modalOpen = true;
  const modal = $('#modal');
  const sheet = $('#modalSheet');
  sheet.innerHTML = html;
  modal.hidden = false;
  if (mount) mount(sheet);
  modal.onclick = (e) => { if (e.target === modal) closeModal(); };
  $$('[data-mclose]', sheet).forEach((b) => { b.onclick = closeModal; });
}
function closeModal() {
  if (!S.modalOpen) return;
  S.modalOpen = false;
  $('#modal').hidden = true;
  $('#modalSheet').innerHTML = '';
  S.overlayDepth = Math.max(0, S.overlayDepth - 1);
  if (S.overlayDepth === 0) restoreStageFromOverlay();
}

function paletteActions() {
  const room = selectedRoom();
  const list = [
    { id: 'hud', label: '打开记录卡', hint: '在录吗、录了多久、最近说了什么', key: '0', icon: '●', run: () => switchView('hud') },
    { id: 'notif', label: '打开通知', hint: '断流、恢复、笔记生成', icon: '◔', run: () => { toggleNotifDrawer(true); } },
    { id: 'listen', label: '收听工作区', hint: '看所有在听、切换直播间', key: '1', icon: '◉', run: () => switchView('listen') },
    { id: 'workbench', label: '工作台', hint: '场次、字幕、AI 总结', key: '2', icon: '▤', run: () => switchView('workbench') },
    { id: 'files', label: '文件中心', hint: '录音、回放、导出', key: '3', icon: '▧', run: () => switchView('files') },
    { id: 'follows', label: '关注', hint: '开播提醒与自动记录', icon: '◔', run: () => switchView('follows') },
    { id: 'library', label: '笔记库', hint: '每一场的要点与高光', icon: '▤', run: () => switchView('library') },
    { id: 'ask', label: '问笔记', hint: '用一句话问你的笔记，答案带出处', icon: '✦', run: () => switchView('library', { search: 'ask' }) },
    { id: 'weekly', label: '周报', hint: '这一周的回看', icon: '▦', run: () => switchView('weekly') },
    { id: 'tools', label: '内容助手', hint: '多人圆桌摘要、上麦者名片', icon: '✧', run: () => switchView('tools') },
    { id: 'delivery', label: '精修交付', hint: '报告、质检与交付包', icon: '▥', run: () => switchView('delivery') },
    { id: 'cleanup', label: '体检清理', hint: '演示 / 空采集 / 重复场次', icon: '⌫', run: () => switchView('files', { tab: 'cleanup' }) },
    { id: 'trash', label: '回收站', hint: '还原或彻底删除', icon: '▤', run: () => switchView('files', { tab: 'trash' }) },
    { id: 'people', label: '人物档案', hint: '声纹与人名确认', key: '4', icon: '◎', run: () => switchView('people') },
    { id: 'settings', label: '设置', hint: '采集、模型、桌面选项', key: '5', icon: '⚙', run: () => switchView('settings') },
    { id: 'add', label: '添加直播间', hint: '粘贴抖音 / 快手 / B 站链接', icon: '＋', run: () => { switchView('listen'); openAdd(); } },
    { id: 'wall', label: '打开监控墙', hint: '所有直播间一屏看状态', icon: '▦', run: () => { switchView('listen'); setStage('wall'); } },
  ];
  if (room) {
    list.push({ id: 'annotate', label: '打开标注模式', hint: '直播页 + 全高字幕，边听边认人', icon: '◎', run: () => { switchView('listen'); setStage('annotate'); } });
    list.push({ id: 'page', label: S.stage === 'page' ? '返回信号画布' : '显示直播页面', hint: roomName(room), icon: '▭', run: () => { switchView('listen'); setTimeout(() => setStage(S.stage === 'page' ? 'signal' : 'page'), 0); } });
    if (room.session_id) list.push({ id: 'live', label: '打开实时字幕', hint: roomName(room), icon: '≡', run: () => switchView('live', { session: room.session_id }) });
  }
  if (S.rooms.some((r) => stateInfo(r).k !== 'stopped')) {
    list.push({ id: 'stopall', label: '停止全部采集', hint: liveRooms().length + ' 路在听', icon: '■', run: () => { closePalette(); setTimeout(stopAll, 0); } });
  }
  list.push({ id: 'refresh', label: '刷新数据', hint: '重新读取房间与会话', icon: '↻', run: () => { refreshRooms(); loadSessions(); toast('已刷新。'); } });
  return list;
}
function openPalette() {
  if (S.paletteOpen || S.modalOpen) return;
  S.overlayDepth++;
  if (S.overlayDepth === 1) hideStageForOverlay();
  S.paletteOpen = true;
  S.paletteIndex = 0;
  S.paletteSearch = { q: '', rows: [], seq: 0, timer: 0 };
  const p = $('#palette');
  p.hidden = false;
  const input = $('#pInput');
  input.value = '';
  input.placeholder = '搜索笔记、原话，或输入命令：收听、工作台、添加直播间…';
  renderPalette();
  setTimeout(() => input.focus(), 20);
}
function closePalette() {
  if (!S.paletteOpen) return;
  S.paletteOpen = false;
  $('#palette').hidden = true;
  S.overlayDepth = Math.max(0, S.overlayDepth - 1);
  if (S.overlayDepth === 0) restoreStageFromOverlay();
}
function searchPaletteRows(q) {
  const seq = ++S.paletteSearch.seq;
  S.paletteSearch.q = q;
  S.paletteSearch.rows = [];
  if (q.length < 2) return;
  api('/api/search?types=notes,utterances&limit=12&q=' + encodeURIComponent(q))
    .then((d) => {
      if (seq !== S.paletteSearch.seq || !S.paletteOpen) return;
      S.paletteSearch.rows = (d.rows || []).map((row) => {
        const t = row.t_start != null ? row.t_start : row.t;
        return {
          id: 'hit-' + row.kind + '-' + row.id,
          label: row.session_title || '笔记',
          hint: (row.speaker_label ? row.speaker_label + ' · ' : '') +
            String(row.text || '').slice(0, 70),
          icon: row.kind === 'utterance' ? '≡' : '▤',
          run: () => openNoteFor(row.session_id, t),
        };
      });
      S.paletteIndex = Math.min(S.paletteIndex, Math.max(0, S.paletteActions.length - 1));
      renderPalette();
    })
    .catch(() => {});
}
function renderPalette() {
  const q = ($('#pInput').value || '').trim().toLowerCase();
  const cmds = paletteActions().filter((a) =>
    !q || (a.label + ' ' + a.hint + ' ' + a.id).toLowerCase().includes(q));
  const hits = (q.length >= 2 && S.paletteSearch.q === q)
    ? S.paletteSearch.rows : [];
  S.paletteActions = cmds.concat(hits);
  const list = $('#pList');
  if (!S.paletteActions.length) {
    list.innerHTML = '<div class="p-empty">没有匹配的命令或笔记。试试主播名、关键词，或“收听”。</div>';
    return;
  }
  S.paletteIndex = clamp(S.paletteIndex, 0, S.paletteActions.length - 1);
  list.innerHTML = S.paletteActions.map((a, i) =>
    '<button class="p-item' + (i === S.paletteIndex ? ' on' : '') + '" data-pi="' + i + '">' +
    '<span class="pi-icon">' + a.icon + '</span>' +
    '<span class="pi-main"><b>' + esc(a.label) + '</b><span>' + esc(a.hint || '') + '</span></span>' +
    (a.key ? '<span class="pi-key">Ctrl ' + a.key + '</span>' : '') + '</button>').join('');
  const on = list.querySelector('.p-item.on');
  if (on) on.scrollIntoView({ block: 'nearest' });
}
function runPalette(i) {
  const a = S.paletteActions[i];
  if (!a) return;
  closePalette();
  setTimeout(() => a.run(), 0);
}

/* ================================================================ add box */
function openAdd() {
  S.addOpen = true;
  $('#addBox').classList.add('on');
  const input = $('#addInput');
  input.focus();
  input.select();
}
function closeAdd() {
  S.addOpen = false;
  $('#addBox').classList.remove('on');
  $('#addInput').value = '';
}

/* ================================================================ scope */
const scope = { ctx: null, w: 0, h: 0, raf: 0 };
function resizeScope() {
  const c = $('#scope');
  if (!c) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const r = c.getBoundingClientRect();
  if (!r.width || !r.height) return;
  scope.w = r.width; scope.h = r.height;
  c.width = Math.round(r.width * dpr);
  c.height = Math.round(r.height * dpr);
  scope.ctx = c.getContext('2d');
  scope.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
function drawScope(now) {
  scope.raf = requestAnimationFrame(drawScope);
  const room = selectedRoom();
  if (!room || S.view !== 'listen' || S.stage !== 'signal' || S.pageOpen || document.hidden) return;
  if (!scope.ctx) resizeScope();
  const ctx = scope.ctx;
  if (!ctx) return;
  const w = scope.w, h = scope.h;
  if (!w || !h) return;
  const dark = resolvedTheme() === 'dark';
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = dark ? '#070B0F' : '#F7F8F9';
  ctx.fillRect(0, 0, w, h);

  ctx.strokeStyle = dark ? 'rgba(147,184,222,.055)' : 'rgba(90,101,114,.10)';
  ctx.lineWidth = 1;
  for (let x = 0; x < w; x += 44) {
    ctx.beginPath(); ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, h); ctx.stroke();
  }
  for (let y = 0; y < h; y += 36) {
    ctx.beginPath(); ctx.moveTo(0, y + 0.5); ctx.lineTo(w, y + 0.5); ctx.stroke();
  }
  ctx.strokeStyle = dark ? 'rgba(147,184,222,.13)' : 'rgba(90,101,114,.18)';
  ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();

  const s = sessionFor(room);
  const live = stateInfo(room).k === 'live';
  const t = now / 1000;
  const amp = (live ? 0.55 + Math.min(0.35, Number(s && s.level) || 0.1) : 0.12) * h * 0.32;
  const phase = t * (live ? 1.6 : 0.25);
  const seed = String(room.id).split('').reduce((a, c) => a + c.charCodeAt(0), 0) % 100;

  function line(color, alpha, mult, speed) {
    ctx.beginPath();
    for (let x = 0; x <= w; x += 2) {
      const p = x / w;
      const env = 0.25 + 0.75 * Math.exp(-Math.pow((p - 0.5) * 1.7, 2));
      const v = (Math.sin(p * 34 + phase * speed + seed) * 0.55 +
        Math.sin(p * 91 - phase * 0.7 + seed * 0.3) * 0.24 +
        Math.sin(p * 13 + phase * 0.4) * 0.2) * amp * env * mult;
      const y = h / 2 + v;
      if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = color;
    ctx.globalAlpha = alpha;
    ctx.lineWidth = 1.4;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }
  line(dark ? 'rgba(230,181,102,.85)' : 'rgba(138,90,14,.72)', 0.85, 1, 1);
  line(dark ? 'rgba(143,180,217,.55)' : 'rgba(63,111,158,.45)', 0.5, 1.35, 1.13);

  const px = ((now / 26) % (w + 120)) - 60;
  const grad = ctx.createLinearGradient(px - 18, 0, px + 18, 0);
  const sweep = dark ? '231,182,105' : '138,90,14';
  grad.addColorStop(0, 'rgba(' + sweep + ',0)');
  grad.addColorStop(0.5, dark ? 'rgba(231,182,105,.18)' : 'rgba(138,90,14,.12)');
  grad.addColorStop(1, 'rgba(' + sweep + ',0)');
  ctx.fillStyle = grad;
  ctx.fillRect(px - 18, 0, 36, h);
  ctx.strokeStyle = dark ? 'rgba(230,181,102,.5)' : 'rgba(138,90,14,.35)';
  ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
}
function startScope() {
  cancelAnimationFrame(scope.raf);
  scope.raf = requestAnimationFrame(drawScope);
}

/* ================================================================ navigate */
function navigateToUrl(href) {
  let u;
  try { u = new URL(href, API + '/'); } catch (e) { return; }
  if (u.origin !== ENGINE_ORIGIN) { toast('外部页面请在应用内浏览器打开。'); return; }
  const path = u.pathname.replace(/\/+$/, '') || '/';
  const sid = u.searchParams.get('session') || u.searchParams.get('session_id') || '';
  if (path === '/rooms') return switchView('listen');
  if (path === '/live') return switchView('live', { session: sid });
  if (path === '/note') {
    return switchView('note', { session: sid, t: u.searchParams.get('t') });
  }
  if (path === '/follows') return switchView('follows');
  if (path === '/weekly') return switchView('weekly', { period: u.searchParams.get('period') });
  if (path === '/delivery') return switchView('delivery', { id: u.searchParams.get('id') });
  if (path === '/library') {
    return switchView('library', {
      search: u.searchParams.get('search'), q: u.searchParams.get('q'),
    });
  }
  if (path === '/files') {
    const tab = u.searchParams.get('tab') || 'live';
    return switchView('files', { tab });
  }
  if (path === '/people') return switchView('people');
  if (path === '/tools') return switchView('tools');
  if (path === '/' || path === '/index.html') {
    if (u.searchParams.get('settings') != null) return switchView('settings');
    if (sid) S.selectedSession = sid;
    const t = u.searchParams.get('t');
    return switchView('workbench', { session: sid, t });
  }
  const embed = new URL(u.href);
  embed.searchParams.set('embed', '1');
  return switchView('frame', {
    url: embed.href, rawUrl: u.href,
    title: ({ '/reader': '阅读', '/highlights': '高光榜', '/metrics': '指标' })[path] || '页面',
    meta: sid ? shortSid(sid) : '', back: 'workbench',
  });
}

/* ================================================================ bind */
/* ================================================================ 通知
   引擎产事件，壳决定展示：前台用应用内提示，后台才发 Windows toast。
   同一行数据也进通知中心；重启后旧通知只留在中心，不再弹一遍。 */
function notifClock(iso) {
  const m = String(iso || '').match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!m) return '';
  const today = new Date();
  const sameDay = Number(m[2]) === today.getMonth() + 1 && Number(m[3]) === today.getDate();
  return sameDay ? (m[4] + ':' + m[5]) : (m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5]);
}
function notifActionLabel(item) {
  const v = (item.action || {}).view;
  return {
    hud: '打开记录卡', note: '打开笔记', library: '打开笔记库',
    follow: '查看关注', follows: '查看关注', weekly: '打开周报',
    delivery: '打开交付', people: '打开人物档案',
    storage: '打开存储设置', diagnostics: '检查问题',
  }[v] || '查看';
}
function renderNotifBadge() {
  const n = Number(S.notif.unread) || 0;
  const dot = $('#notifDot');
  dot.hidden = n <= 0;
  dot.textContent = n > 9 ? '9+' : String(n);
  $('#notifBtn').title = n ? (n + ' 条未读通知') : '通知';
  const un = $('#notifUnread');
  un.hidden = n <= 0;
  un.textContent = n + ' 条未读';
}
function renderNotifDrawer() {
  if (!S.notif.open) return;
  const list = $('#notifList');
  const items = (S.notif.items || []).slice().sort((a, b) => b.id - a.id);
  if (!items.length) {
    list.innerHTML = '<div class="notif-empty">还没有通知。<br>断流、恢复、笔记生成都会记在这里。</div>';
  } else {
    list.innerHTML = items.map((x) => {
      const cls = [x.read ? 'read' : 'unread', x.level || 'info'].join(' ');
      return '<button class="notif-item ' + cls + '" data-nid="' + x.id + '">' +
        '<span class="nt">' + esc(x.title) + '</span>' +
        (x.body ? '<span class="nb">' + esc(x.body) + '</span>' : '') +
        '<span class="nm"><i></i>' + esc(notifClock(x.created_at)) +
        '<span>·</span><span>' + esc(notifActionLabel(x)) + '</span></span></button>';
    }).join('');
  }
  renderNotifBadge();
  renderNotifPrefs();
}
function renderNotifPrefs() {
  const box = $('#npBody');
  if (!box) return;
  const st = S.notif.settings;
  if (!st) { box.innerHTML = '<div class="np-row"><span>正在读取通知设置…</span></div>'; return; }
  const muted = new Set(st.muted || []);
  const rows = [
    '<label class="np-row"><input type="checkbox" data-np="enabled"' +
      (st.enabled ? ' checked' : '') + '><span class="grow"><b>允许通知</b><br><span>关掉后不再产生新通知，历史仍保留</span></span></label>',
    '<label class="np-row"><input type="checkbox" data-np="system"' +
      (st.system ? ' checked' : '') + (st.enabled ? '' : ' disabled') +
      '><span class="grow"><b>后台用系统通知</b><br><span>窗口不在前台时弹 Windows 通知</span></span></label>',
    '<label class="np-row"><input type="checkbox" data-np="detail"' +
      (st.detail ? ' checked' : '') + (st.enabled ? '' : ' disabled') +
      '><span class="grow"><b>显示直播标题</b><br><span>关掉后只说「你关注的主播」「一场直播」</span></span></label>',
    '<label class="np-row"><input type="checkbox" data-np="insights"' +
      (st.insights ? ' checked' : '') + (st.enabled ? '' : ' disabled') +
      '><span class="grow"><b>主动提醒</b><br><span>观点反转、老问题有答案、常客上麦；每场最多一条，默认关</span></span></label>',
  ];
  if (S.notif.sysDenied) {
    rows.push('<div class="np-row"><span style="color:var(--amber)">系统通知权限被关闭了，应用内通知中心仍然完整。</span></div>');
  }
  rows.push('<div class="np-sep"></div>');
  const groups = new Map();
  (st.kinds || []).forEach((k) => {
    const g = k.group || '其他';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(k);
  });
  groups.forEach((list, group) => {
    rows.push('<div class="np-group">' + esc(group) + '</div>');
    list.forEach((k) => {
      const off = !st.enabled ||
        (String(k.kind).indexOf('insight_') === 0 && !st.insights);
      rows.push('<label class="np-row"><input type="checkbox" data-np="kind" data-kind="' +
        esc(k.kind) + '"' + (muted.has(k.kind) ? '' : ' checked') +
        (off ? ' disabled' : '') + '><span class="grow"><b>' + esc(k.label) +
        '</b><br><span>' + esc(k.desc) + '</span></span></label>');
    });
  });
  rows.push('<button class="btn np-test" id="npTest">发一条测试通知</button>');
  box.innerHTML = rows.join('');
}
async function pollNotifications() {
  if (S.notif.busy) return;
  if (!API || !S.online) { setTimeout(pollNotifications, 5000); return; }
  S.notif.busy = true;
  const first = !S.notif.booted;
  try {
    const d = await api('/api/notifications?since_id=' +
      encodeURIComponent(first ? 0 : S.notif.latestId) + '&limit=50');
    S.notif.settings = d.settings || S.notif.settings;
    const items = Array.isArray(d.items) ? d.items : [];
    const seen = new Set((S.notif.items || []).map((x) => x.id));
    const fresh = [];
    items.forEach((x) => {
      if (!seen.has(x.id)) { S.notif.items.push(x); seen.add(x.id); }
      if (!x.delivered) fresh.push(x);
    });
    S.notif.items = S.notif.items.slice(-100);
    S.notif.latestId = Math.max(Number(S.notif.latestId) || 0, Number(d.latest_id) || 0);
    S.notif.unread = Number(d.unread) || 0;

    if (first) {
      // 应用启动：只填中心，不把历史通知重播成系统 toast。
      const ids = items.map((x) => x.id);
      if (ids.length) post('/api/notifications/delivered', { ids }).catch(() => {});
      items.forEach((x) => { x.delivered = true; });
      S.notif.booted = true;
    } else {
      const shown = [];
      for (const x of fresh) {
        const ok = await presentNotification(x);
        if (ok) shown.push(x.id);
      }
      if (shown.length) post('/api/notifications/delivered', { ids: shown }).catch(() => {});
    }
    renderNotifBadge();
    if (S.notif.open) renderNotifDrawer();
  } catch (e) {
    /* 引擎重启/网络抖动：下一拍再试 */
  } finally {
    S.notif.busy = false;
    setTimeout(pollNotifications, S.online ? perfNum('notify_poll_s', 4) * 1000 : 6000);
  }
}
async function presentNotification(item) {
  const foreground = !document.hidden && document.hasFocus();
  S.notif.lastPresent = { id: item.id, at: Date.now(), foreground,
                          hidden: document.hidden, focus: document.hasFocus() };
  if (foreground) {
    inAppNotice(item);
    return true;
  }
  if (S.notif.settings && S.notif.settings.system === false) {
    S.notif.lastPresent.result = 'system-off';
    return false;
  }
  const ok = await osNotify(item);
  S.notif.lastPresent.result = ok ? 'system' : 'system-failed';
  return ok;
}
function inAppNotice(item) {
  const stack = $('#noticeStack');
  const el = document.createElement('div');
  el.className = 'notice ' + (item.level || 'info');
  el.innerHTML = '<span class="nt">' + esc(item.title) + '</span>' +
    (item.body ? '<span class="nb">' + esc(item.body) + '</span>' : '') +
    '<span class="nx">' + esc(notifActionLabel(item)) + ' →</span>';
  el.onclick = () => { remove(); routeNotification(item); };
  const remove = () => { clearTimeout(el._t); el.remove(); };
  el._t = setTimeout(remove, 5200);
  stack.appendChild(el);
  while (stack.children.length > 3) stack.firstElementChild.remove();
}
async function osNotify(item) {
  if (!TAURI || !TAURI.core || !TAURI.core.invoke) {
    S.notif.lastOs = { id: item.id, result: 'no-ipc' };
    return false;
  }
  try {
    // 系统 toast 走 Rust 插件 builder：Web Notification 的权限门是给网页
    // 通知用的，窗口收起时 requestPermission 可能什么也不显示，反而把
    // 真正能送达的 Windows 通知挡掉。失败（系统关闭通知/无应用标识）由
    // Rust 命令返回错误，这里只记录状态给设置页看。
    await invoke('notify_system', {
      id: Number(item.id) || 0,
      title: String(item.title || 'LiveNotes'),
      body: String(item.body || ''),
    });
    S.notif.sysDenied = false;
    S.notif.lastOs = { id: item.id, result: 'sent' };
    return true;
  } catch (e) {
    S.notif.sysDenied = true;
    S.notif.lastOs = { id: item.id, result: 'error', error: String(e) };
    if (S.notif.open) renderNotifPrefs();
    return false;
  }
}
async function markNotifRead(ids, all) {
  try {
    const d = await post('/api/notifications/read',
      all ? { all: true } : { ids: ids || [] });
    S.notif.unread = Number(d.unread) || 0;
    const set = new Set(ids || []);
    S.notif.items.forEach((x) => { if (all || set.has(x.id)) x.read = true; });
    renderNotifBadge();
    if (S.notif.open) renderNotifDrawer();
  } catch (e) {}
}
function routeNotification(item) {
  markNotifRead([item.id], false);
  closeNotifDrawer();
  const act = item.action || {};
  const sid = act.session_id || item.session_id || '';
  const view = act.view || '';
  if (view === 'hud') {
    if (sid) {
      S.hudSid = sid;
      const r = S.rooms.find((x) => x.session_id === sid);
      if (r) S.hudRoomId = r.id;
    }
    if (S.finish && (!sid || S.finish.sid === sid)) {
      switchView('hud', { force: true, session: sid });
    } else if (sid && !S.rooms.some((x) => x.session_id === sid)) {
      openNoteFor(sid);
    } else {
      switchView('hud', { force: true, session: sid });
    }
    return;
  }
  if (view === 'note') {
    if (S.finish && S.finish.sid === sid) switchView('hud', { force: true, session: sid });
    else if (sid) openNoteFor(sid);
    else switchView('workbench');
    return;
  }
  if (view === 'follow' || view === 'follows') {
    switchView('follows');
    return;
  }
  if (view === 'weekly') {
    switchView('weekly', { period: act.period });
    return;
  }
  if (view === 'delivery') {
    switchView('delivery');
    return;
  }
  if (view === 'library') {
    switchView('library');
    return;
  }
  if (view === 'people') {
    switchView('people');
    return;
  }
  if (view === 'storage') {
    switchView('settings');
    toast('设置页里可以查看磁盘和存储选项。');
    return;
  }
  if (view === 'diagnostics') {
    invoke('diag_open').catch(() => switchView('settings'));
    return;
  }
  if (view === 'home') { switchView('library'); return; }
  if (sid) {
    if (act.t != null) switchView('workbench', { session: sid, autoplay: true, t: act.t });
    else openNoteFor(sid);
  } else {
    switchView('workbench');
  }
}
async function routeSystemNotification(id) {
  const nid = Number(id) || 0;
  if (!nid) return;
  let item = (S.notif.items || []).find((x) => Number(x.id) === nid) || null;
  if (!item) {
    // 应用重启后点击上一轮留在操作中心的 toast：内存里没有，补一次台账。
    try {
      const d = await api('/api/notifications?since_id=0&limit=100');
      S.notif.settings = d.settings || S.notif.settings;
      S.notif.items = (d.items || []).slice(-100);
      S.notif.unread = Number(d.unread) || 0;
      item = S.notif.items.find((x) => Number(x.id) === nid) || null;
      renderNotifBadge();
    } catch (e) {}
  }
  if (item) routeNotification(item);
  else switchView('workbench');
}
async function toggleNotifDrawer(open) {
  S.notif.open = open == null ? !S.notif.open : !!open;
  const el = $('#notifDrawer');
  el.hidden = !S.notif.open;
  if (S.notif.open) {
    renderNotifDrawer();
    // 打开中心不等于已读：用户还要点具体条目；提供「全部已读」一键处理。
    try {
      const d = await api('/api/notifications?since_id=0&limit=50');
      S.notif.settings = d.settings || S.notif.settings;
      S.notif.latestId = Math.max(Number(S.notif.latestId) || 0, Number(d.latest_id) || 0);
      S.notif.unread = Number(d.unread) || 0;
      const seen = new Set(S.notif.items.map((x) => x.id));
      (d.items || []).forEach((x) => { if (!seen.has(x.id)) S.notif.items.push(x); });
      S.notif.items = S.notif.items.slice(-100);
      renderNotifDrawer();
    } catch (e) { renderNotifDrawer(); }
  }
}
function closeNotifDrawer() { toggleNotifDrawer(false); }
async function updateNotifSettings(patch) {
  try {
    const st = await post('/api/notifications/settings', patch);
    S.notif.settings = st;
    renderNotifPrefs();
  } catch (e) {
    toast('通知设置没保存成功：' + e.message);
  }
}

/* 托盘菜单动作：Rust 只发意图，真正的 /api/live/* 由壳页调，窗口隐藏时脚本仍活着。 */
function latestLiveRoom() {
  const live = liveRooms();
  return live.length ? live[live.length - 1] : null;
}
function focusHudRoom(room) {
  if (!room) return false;
  S.hudRoomId = room.id;
  if (room.session_id) S.hudSid = room.session_id;
  return true;
}
/* 「进入记录卡」什么时候有意义：有会话（在录 / 已暂停 / 收尾中）才需要那张卡；
   只有已停止且没有会话的一路不该把用户骗进去。 */
function canEnterHud(room) {
  if (!room || !room.session_id) return false;
  const k = stateInfo(room).k;
  return k === 'live' || k === 'pending';
}
/* 从收听页「选一路 → 进记录卡」。导航栏的 switchView('hud') 在多路时会被拦下
   （见 switchView 里的多路分支），这条路径必须带上具体房间，否则记录卡还是
   会停在上一路。从记录卡切回多路时也用它把收听页选中项落回刚看的那一路。 */
function openHudFor(room) {
  if (!room) { toast('先选一路直播间。'); return false; }
  if (!room.session_id && stateInfo(room).k === 'stopped') {
    toast('这一路已经结束，没有可继续记录的会话。');
    return false;
  }
  if (S.pageOpen) closeStageView();
  const changed = S.hudRoomId !== room.id;
  focusHudRoom(room);
  S.selectedId = room.id;
  S.selectedSid = room.session_id || '';
  // 记录卡和收听页共用 S.lines；换一路要立刻清掉上一路的字幕，
  // 否则进卡的第一拍会先闪出另一路的句子。
  if (changed) { S.lines = []; S.speakers = []; S.speakersSid = ''; }
  switchView('hud', { force: true, room: room.id });
  return true;
}
/* 记录卡里的「切换到多路」：回到收听页时把选中项落回刚看的那一路，
   不然用户会看到上一次点过的另一路，以为记录卡记错了对象。 */
function backToMulti() {
  const room = hudRoom() || selectedRoom();
  if (room) {
    S.selectedId = room.id;
    S.selectedSid = room.session_id || '';
  }
  switchView('listen', room ? { room: room.id } : undefined);
}
async function handleTrayAction(action) {
  const room = latestLiveRoom() || hudRoom();
  if (action === 'hud') {
    if (room) focusHudRoom(room);
    switchView('hud', { force: true, room: room ? room.id : undefined });
    return;
  }
  if (action === 'pause' || action === 'resume') {
    if (!room || !room.session_id) return;
    try {
      await post('/api/live/pause', {
        session_id: room.session_id, paused: action === 'pause',
      });
      toast(action === 'pause' ? '已暂停记录。' : '已继续记录。');
      if (S.view === 'hud') await pollTranscript();
      else await refreshRooms();
    } catch (e) { toast('操作失败：' + e.message); }
    return;
  }
  if (action === 'mark') {
    if (!room || !room.session_id) return;
    try {
      await post('/api/live/mark', { session_id: room.session_id, note: '托盘打点' });
      toast('已标记此刻。');
      if (S.view === 'hud') await pollTranscript();
    } catch (e) { toast('打点失败：' + e.message); }
    return;
  }
  if (action === 'finish') {
    if (!room) { toast('现在没有在记录的直播间。'); return; }
    focusHudRoom(room);
    switchView('hud', { force: true, room: room.id });
    confirmFinish(room);
    return;
  }
  if (action === 'recent_note') {
    await openRecentNote();
  }
}
async function openRecentNote() {
  try {
    const d = await api('/api/sessions');
    const rows = Array.isArray(d.sessions) ? d.sessions : [];
    const best = rows.find((s) => Number(s.n_utterances) > 0) || rows[0];
    if (best) openNoteFor(best.id);
    else { switchView('workbench'); toast('还没有笔记。开始记录一场试试。'); }
  } catch (e) {
    switchView('workbench');
  }
}

function bind() {
  document.addEventListener('click', (e) => {
    if (S.notif.open && !e.target.closest('#notifDrawer, #notifBtn')) closeNotifDrawer();
    const rail = e.target.closest('.rail-item');
    if (rail) {
      const v = rail.dataset.view;
      if (v === 'settings') switchView('settings');
      else switchView(v);
      return;
    }
  });

  document.addEventListener('mousedown', (e) => {
    if (!S.spkPop) return;
    if (e.target.closest('#spkPop') || e.target.closest('[data-spk-id]') ||
        e.target.closest('[data-spk-act]')) return;
    closeSpeakerPop();
  });

  $('#btnAdd').onclick = () => { S.addOpen ? closeAdd() : openAdd(); };
  $('#emptyAdd').onclick = () => { switchView('listen'); openAdd(); };
  $('#addGo').onclick = addRoom;
  $('#addCancel').onclick = closeAdd;
  $('#addInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addRoom(); }
    if (e.key === 'Escape') { e.preventDefault(); closeAdd(); }
  });

  $('#sideTools').addEventListener('click', (e) => {
    const toggle = e.target.closest('[data-batch-toggle]');
    if (toggle) {
      S.batchMode = !S.batchMode;
      S.batchSel.clear();
      renderSidebar();
      return;
    }
    if (e.target.closest('[data-batch-all]')) {
      if (S.batchSel.size === S.sessions.length) S.batchSel.clear();
      else S.sessions.slice(0, 100).forEach((s) => S.batchSel.add(s.id));
      renderSidebar();
      return;
    }
    const b = e.target.closest('[data-filter]');
    if (!b) return;
    S.filter = b.dataset.filter;
    renderSidebar();
  });
  $('#sideList').addEventListener('click', (e) => {
    const play = e.target.closest('[data-play]');
    if (play) { e.preventDefault(); e.stopPropagation(); openWorkbenchPlay(play.dataset.play); return; }
    const more = e.target.closest('[data-more]');
    if (more) {
      e.preventDefault(); e.stopPropagation();
      const rect = more.getBoundingClientRect();
      if (more.dataset.more === 'session') sessionMenu(more.dataset.id, rect.left, rect.bottom + 4);
      else roomMenu(more.dataset.id, rect.left, rect.bottom + 4);
      return;
    }
    const demo = e.target.closest('[data-demo]');
    if (demo) { e.preventDefault(); e.stopPropagation(); loadDemo(); return; }
    const r = e.target.closest('[data-room]');
    if (r) return selectRoom(r.dataset.room);
    const s = e.target.closest('[data-session]');
    if (s) {
      if (S.batchMode && S.view === 'workbench') {
        if (S.batchSel.has(s.dataset.session)) S.batchSel.delete(s.dataset.session);
        else S.batchSel.add(s.dataset.session);
        renderSidebar();
        return;
      }
      S.selectedSession = s.dataset.session;
      switchView('workbench', { session: S.selectedSession });
    }
  });
  $('#sideList').addEventListener('contextmenu', (e) => {
    const s = e.target.closest('[data-session]');
    const r = e.target.closest('[data-room]');
    if (!s && !r) return;
    e.preventDefault();
    e.stopPropagation();
    if (s) sessionMenu(s.dataset.session, e.clientX, e.clientY);
    else roomMenu(r.dataset.room, e.clientX, e.clientY);
  });
  $('#sideFoot').addEventListener('click', (e) => {
    if (e.target.closest('[data-clear-stopped]')) clearStoppedRooms();
    if (e.target.closest('[data-open-trash]')) switchView('files', { tab: 'trash' });
    if (e.target.closest('[data-batch-trash]')) confirmBatchTrash();
    if (e.target.closest('[data-batch-export]')) batchExport();
    if (e.target.closest('[data-batch-cancel]')) {
      S.batchMode = false; S.batchSel.clear(); renderSidebar();
    }
  });
  $('#wall').addEventListener('click', (e) => {
    if (e.target.closest('[data-add]')) { switchView('listen'); openAdd(); return; }
    const r = e.target.closest('[data-room]');
    if (!r) return;
    S.stage = 'signal';
    selectRoom(r.dataset.room);
    renderStage();
  });

  $('#stageSeg').addEventListener('click', (e) => {
    const b = e.target.closest('[data-stage]');
    if (!b) return;
    setStage(b.dataset.stage);
  });
  $('#btnWall').onclick = () => {
    if (S.stage === 'wall') setStage('signal');
    else setStage('wall');
  };
  $('#btnStop').onclick = () => confirmStopRoom(selectedRoom());
  $('#stageRetry').onclick = () => retryRoom(selectedRoom());
  $('#stagePause').onclick = () => togglePause();
  $('#stageMarkBtn').onclick = () => markMoment();
  $('#multiBarGo').onclick = () => openHudFor(selectedRoom());
  $('#multiRooms').addEventListener('click', (e) => {
    const b = e.target.closest('[data-mb-room]');
    if (!b) return;
    openHudFor(findRoom(b.dataset.mbRoom));
  });
  $('#emptyDemoBtn').onclick = () => loadDemo();
  $('#marksList').addEventListener('click', (e) => {
    const b = e.target.closest('[data-mark-seek]');
    const room = selectedRoom();
    if (!b || !room || !room.session_id) return;
    switchView('workbench', { session: room.session_id, autoplay: true, t: Number(b.dataset.markSeek) || 0 });
  });

  /* 原生工作台：时间轴、筛选、字幕跳转、播放器、总结标签 */
  $('#wbTlBars').addEventListener('click', (e) => {
    const b = e.target.closest('[data-t]');
    if (b) wbSeek(Number(b.dataset.t) || 0);
  });
  $('#wbSpkFilters').addEventListener('click', (e) => {
    const b = e.target.closest('[data-spk-filter]');
    if (!b) return;
    S.wb.spk = b.dataset.spkFilter;
    renderWbFilters();
    renderWbTranscript();
  });
  $('#wbHighOnly').addEventListener('change', (e) => {
    S.wb.high = !!e.target.checked;
    renderWbTranscript();
  });
  $('#wbTranscript').addEventListener('click', (e) => {
    const b = e.target.closest('[data-seek]');
    if (!b) return;
    wbSeek(Number(b.dataset.seek) || 0, { play: true });
  });
  $('#wbPlay').onclick = wbTogglePlay;
  $('#wbRange').addEventListener('input', (e) => {
    const t = Number(e.target.value) || 0;
    S.wb.currentT = t;
    $('#wbCur').textContent = fmtDur(t);
    const audio = $('#wbAudio');
    if (S.wb.audioReady) { try { audio.currentTime = t; } catch (err) {} }
  });
  const wbSyncPlay = () => {
    const on = !$('#wbAudio').paused;
    // SVGElement 没有 hidden 这个 IDL 属性：写 `.hidden = true` 只是挂了个
    // JS 自定义属性，不会动 HTML 里的 hidden 特性，图标自然永远不切换。
    // 必须用 toggleAttribute 改特性。
    const btn = $('#wbPlay');
    btn.querySelector('.p-play').toggleAttribute('hidden', on);
    btn.querySelector('.p-pause').toggleAttribute('hidden', !on);
    btn.title = on ? '暂停' : '播放';
    btn.setAttribute('aria-label', on ? '暂停' : '播放');
  };
  $('#wbAudio').addEventListener('timeupdate', () => {
    const audio = $('#wbAudio');
    const t = audio.currentTime || 0;
    S.wb.currentT = t;
    $('#wbCur').textContent = fmtDur(t);
    $('#wbRange').value = String(Math.round(t));
  });
  $('#wbAudio').addEventListener('play', wbSyncPlay);
  $('#wbAudio').addEventListener('pause', wbSyncPlay);
  $('#wbAudio').addEventListener('ended', wbSyncPlay);
  $('#wbExport').onclick = () => { if (S.wb.sid) downloadSession(S.wb.sid); };
  $('#inspScroll').addEventListener('click', (e) => {
    const tab = e.target.closest('[data-sum-tab]');
    if (tab) return setWbSummaryScope(tab.dataset.sumTab);
    const adopt = e.target.closest('[data-title-adopt]');
    if (adopt) return adoptWbTitle(adopt.dataset.titleAdopt);
    const fresh = e.target.closest('[data-title-fresh]');
    if (fresh) return freshWbTitle();
    const gen = e.target.closest('[data-wb-gen]');
    if (gen) return generateWbSummary(gen.dataset.wbGen, gen.dataset.engine);
    const seek = e.target.closest('[data-seek]');
    if (seek) return wbSeek(Number(seek.dataset.seek) || 0, { play: true });
  });
  $('#inspActions').addEventListener('click', (e) => {
    const gen = e.target.closest('[data-wb-gen]');
    if (gen) generateWbSummary(gen.dataset.wbGen, gen.dataset.engine);
  });

  /* 原生设置：分类、搜索、即时保存 */
  $('#setNav').addEventListener('click', (e) => {
    const b = e.target.closest('[data-setpane]');
    if (b) setSettingsPane(b.dataset.setpane);
  });
  $('#setSearch').addEventListener('input', (e) => filterSettings(e.target.value));
  $('#setSearch').addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.target.value = '';
      filterSettings('');
    }
  });
  $('#setContent').addEventListener('change', (e) => {
    const t = e.target;
    if (t.name === 'themeChoice') { setTheme(t.value); return; }
    if (t.id === 'prefFallback') return savePref('direct_fallback', t.value);
    if (t.id === 'prefOpenMode') return savePref('open_mode', t.value);
    if (t.id === 'prefCaptureMode') return savePref('capture_mode', t.value);
    if (t.id === 'prefCloseAction') return savePref('close_action', t.value);
    if (t.id === 'prefLan') return toggleLan(t.checked);
    if (t.name === 'perfMode') {
      $$('.perf-card').forEach((card) => {
        const inp = card.querySelector('input');
        card.classList.toggle('is-active', !!(inp && inp.checked));
      });
      return savePerfMode(t.value);
    }
    if (t.id === 'prefFollowInterval') {
      return savePref('follow_interval_s', clamp((Number(t.value) || 2) * 60, 60, 600));
    }
    if (t.id === 'prefAutoRecord') return savePref('auto_record_enabled', t.checked);
    if (t.id === 'prefWeekly') return savePref('weekly_enabled', t.checked);
    if (t.id === 'prefWeeklyTime') return savePref('weekly_time', t.value);
    if (t.id === 'prefNotify') {
      S.set.prefs = S.set.prefs || {};
      S.set.prefs.notify_enabled = t.checked;
      if (S.notif.settings) S.notif.settings.enabled = t.checked;
      if (S.notif.open) renderNotifPrefs();
      return savePref('notify_enabled', t.checked);
    }
    if (t.id === 'prefAskSuggest') return savePref('qa_suggest_enabled', t.checked);
    if (t.id === 'prefMaxRooms') return savePref('max_rooms', Number(t.value) || 5);
    if (t.id === 'prefLowPower') {
      savePref('low_power_pause', t.checked).then(() => {
        loadPerf();
        renderPerfAdvice();
      });
      return;
    }
    if (t.id === 'prefTopics') {
      const v = clamp(Number(t.value) || 10, 3, 40);
      t.value = String(v);
      return savePref('topics', v);
    }
  });
  $('#setTemp').addEventListener('input', (e) => { $('#setTempVal').value = e.target.value; });
  $('#profileList').addEventListener('click', (e) => {
    const b = e.target.closest('[data-profile]');
    if (b) activateProfile(b.dataset.profile);
  });
  $('#providerChips').addEventListener('click', (e) => {
    const b = e.target.closest('[data-provider-base]');
    if (!b) return;
    $('#setBase').value = b.dataset.providerBase || '';
    $('#setModel').value = b.dataset.providerModel || '';
    $('#connState').className = 'conn-state';
    $('#connState').textContent = '未测试';
  });
  $('#toggleKeyBtn').onclick = () => {
    const k = $('#setKey');
    const show = k.type === 'password';
    k.type = show ? 'text' : 'password';
    $('#toggleKeyBtn').textContent = show ? '隐藏' : '显示';
  };
  $('#copyLanBtn').onclick = () => copyText($('#lanUrl').textContent);
  $('#perfCheckBtn').onclick = async () => {
    await loadPerf();
    renderPerfAdvice();
    renderPerfOut();
  };
  $('#testConnBtn').onclick = testConnection;
  $('#saveConnBtn').onclick = saveConnection;

  // 实时字幕 / 标注：点说话人认人、切筛选、自动滚动
  $('#txTabs').addEventListener('click', (e) => {
    const b = e.target.closest('[data-tab]');
    if (!b) return;
    S.annotTab = b.dataset.tab;
    renderAnnot();
  });
  $('#txAuto').addEventListener('click', () => {
    S.annotAuto = !S.annotAuto;
    $('#txAuto').classList.toggle('on', S.annotAuto);
    if (S.annotAuto) {
      const sc = $('#txScroll');
      sc.scrollTop = sc.scrollHeight;
    }
  });
  $('#txScroll').addEventListener('click', (e) => {
    const chip = e.target.closest('.spk[data-spk-id]');
    if (!chip) return;
    e.preventDefault();
    openSpeakerPop(chip.dataset.spkId, chip);
  });
  $('#txScroll').addEventListener('scroll', () => {
    const sc = $('#txScroll');
    const near = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 48;
    if (near !== S.annotAuto) {
      S.annotAuto = near;
      $('#txAuto').classList.toggle('on', near);
    }
  });
  $('#txBanner').addEventListener('click', (e) => {
    const b = e.target.closest('[data-spk-act]');
    if (!b) return;
    handleSpkAction(b.dataset.spkAct, b.dataset.spk, b);
  });
  $('#spkPop').addEventListener('click', (e) => {
    const b = e.target.closest('[data-target],[data-target-confirm],[data-new],[data-role],[data-reject]');
    if (!b || !S.spkPop) return;
    const cur = S.spkPop.id;
    if (b.dataset.target) {
      const t = spkById(b.dataset.target);
      if (t) mergeSpeakerInto(cur, t);
      return;
    }
    if (b.dataset.targetConfirm) { confirmSpeaker(cur); return; }
    if (b.dataset.new) { promptSpeakerName(cur); return; }
    if (b.dataset.role) { renameSpeaker(cur, b.dataset.role); return; }
    if (b.dataset.reject) { rejectSpeaker(cur); return; }
  });
  $('#inspScroll').addEventListener('click', (e) => {
    const b = e.target.closest('[data-spk-act]');
    if (!b) return;
    handleSpkAction(b.dataset.spkAct, b.dataset.spk, b);
  });
  $('#inspActions').addEventListener('click', (e) => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const room = selectedRoom();
    const act = b.dataset.act;
    if (act === 'hud') return openHudFor(room);
    if (act === 'page') return setStage(S.stage === 'page' ? 'signal' : 'page');
    if (act === 'live' && room && room.session_id) return switchView('live', { session: room.session_id, back: 'listen' });
    if (act === 'retry') return retryRoom(room);
    if (act === 'remove') return confirmRemoveRoom(room);
    if (act === 'stop') return $('#btnStop').click();
  });

  /* 记录卡：暂停 / 标记 / 结束，以及断流时的补救动作。 */
  $('#hudPause').onclick = () => togglePause();
  $('#hudMark').onclick = () => markMoment();
  $('#hudFinish').onclick = () => confirmFinish(hudRoom());
  $('#hudMulti').onclick = () => backToMulti();
  $('#hudStart').onclick = () => { switchView('listen'); openAdd(); };
  $('#hudAlert').addEventListener('click', (e) => {
    const b = e.target.closest('[data-hud-act]');
    if (!b) return;
    const room = hudRoom();
    const act = b.dataset.hudAct;
    if (act === 'retry') return retryRoom(room);
    if (act === 'finish') return confirmFinish(room);
    if (act === 'listen') return switchView('listen');
    if (act === 'live' && room && room.session_id) {
      return switchView('live', { session: room.session_id, back: 'hud' });
    }
  });
  $('#hudMarks').addEventListener('click', (e) => {
    const b = e.target.closest('[data-hud-mark]');
    if (!b || !S.hudSid) return;
    switchView('workbench', { session: S.hudSid, autoplay: true, t: Number(b.dataset.hudMark) || 0 });
  });
  $('#hudHighlight').addEventListener('click', (e) => {
    const b = e.target.closest('[data-hh]');
    if (!b) return;
    const id = $('#hudHighlight').dataset.hhId;
    const list = (S.sessInfo && S.sessInfo.live_highlights) || [];
    const h = list.find((x) => x.id === id);
    if (!h) return;
    const act = b.dataset.hh;
    if (act === 'save') { saveHudHighlight(h); return; }
    if (act === 'skip') {
      S.hudHi.dismissUntil = Date.now() + 120000;
      renderHudHighlight();
      return;
    }
    if (act === 'play') {
      switchView('workbench', {
        session: S.hudSid, autoplay: true, t: Math.max(0, Number(h.t0) - 3),
      });
    }
  });
  $('#fcOpen').onclick = () => { if (S.finish) openNoteFor(S.finish.sid); };
  $('#fcRaw').onclick = () => { if (S.finish) openNoteFor(S.finish.sid); };
  $('#fcLater').onclick = () => dismissFinish();
  $('#fcRetry').onclick = () => {
    const f = S.finish;
    if (!f) return;
    f.failed = ''; f.error = ''; f.done = false; f.job = null; f.polling = false;
    renderFinish();
    if (f.jobId) {
      post('/api/jobs/retry', { job_id: f.jobId }).catch(() => {});
    } else {
      // 收尾任务没排上队（很少见）：重新提交一份自动总结，等它出现在任务列表里。
      post('/api/summarize', {
        session_id: f.sid, mode: 'content', filters: {}, engine: 'auto',
      }).catch(() => {});
    }
    pollFinish();
  };

  /* 通知中心 / 偏好 */
  $('#notifBtn').onclick = (e) => { e.stopPropagation(); toggleNotifDrawer(); };
  $('#notifClose').onclick = () => closeNotifDrawer();
  $('#notifReadAll').onclick = () => markNotifRead(null, true);
  $('#notifList').addEventListener('click', (e) => {
    const b = e.target.closest('[data-nid]');
    if (!b) return;
    const item = S.notif.items.find((x) => x.id === Number(b.dataset.nid));
    if (item) routeNotification(item);
  });
  $('#npBody').addEventListener('change', (e) => {
    const el = e.target;
    if (!el || !el.dataset || !el.dataset.np) return;
    if (el.dataset.np === 'enabled') return updateNotifSettings({ notify_enabled: !!el.checked });
    if (el.dataset.np === 'system') return updateNotifSettings({ notify_system: !!el.checked });
    if (el.dataset.np === 'detail') return updateNotifSettings({ notify_detail: !!el.checked });
    if (el.dataset.np === 'insights') return updateNotifSettings({ notify_insights: !!el.checked });
    if (el.dataset.np === 'kind') {
      const muted = $$('#npBody input[data-np="kind"]')
        .filter((x) => !x.checked).map((x) => x.dataset.kind);
      return updateNotifSettings({ notify_muted: muted });
    }
  });
  $('#npBody').addEventListener('click', (e) => {
    if (!e.target.closest('#npTest')) return;
    post('/api/notifications/test', {}).then(() => {
      pollNotifications();
      toast('测试通知已发出。');
    }).catch((err) => toast('测试通知失败：' + err.message));
  });
  $('#notifPrefs').addEventListener('toggle', () => { if ($('#notifPrefs').open) renderNotifPrefs(); });
  $('#frameBack').onclick = () => switchView(S.frameBackView || 'listen');
  $('#frame').addEventListener('load', () => {
    S.frameLoads += 1;
    if (S.view === 'workbench' && S.frameKind === 'workbench') S.workbenchReady = true;
  });
  $('#frameRaw').onclick = () => {
    S.frameRaw = !S.frameRaw;
    $('#frameRaw').textContent = S.frameRaw ? '收起工具栏' : '原工具栏';
    $('#frame').src = S.frameRaw ? S.frameRawUrl : S.frameEmbedUrl;
  };

  $('#cmdkBtn').onclick = () => {
    // 设置页里这个框就是“搜索设置”，不再弹命令面板。
    if (S.view === 'settings') {
      const s = $('#setSearch');
      if (s) { s.focus(); return; }
    }
    openPalette();
  };
  $('#themeToggle').onclick = toggleTheme;
  $('#btnSettings').onclick = () => switchView('settings');
  $('#recPill').onclick = () => {
    const live = liveRooms();
    if (live.length === 1) {
      // 只有一路时「2 路在听」胶囊就是记录卡的快捷入口。
      openHudFor(live[0]);
      return;
    }
    switchView('listen');
    if (live.length > 1) setStage('wall');
    else setStage(S.stage === 'wall' ? 'signal' : 'wall');
  };
  $('#palette').addEventListener('click', (e) => { if (e.target === $('#palette')) closePalette(); });
  $('#pInput').addEventListener('input', () => {
    S.paletteIndex = 0;
    const q = ($('#pInput').value || '').trim().toLowerCase();
    // 旧结果立即作废，避免输入后马上回车点到上一次的搜索结果。
    S.paletteSearch.q = '';
    S.paletteSearch.rows = [];
    S.paletteSearch.seq += 1;
    clearTimeout(S.paletteSearch.timer);
    renderPalette();
    if (q.length >= 2) {
      S.paletteSearch.timer = setTimeout(() => searchPaletteRows(q), 180);
    }
  });
  $('#pInput').addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); S.paletteIndex = Math.min(S.paletteActions.length - 1, S.paletteIndex + 1); renderPalette(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); S.paletteIndex = Math.max(0, S.paletteIndex - 1); renderPalette(); }
    else if (e.key === 'Enter') { e.preventDefault(); runPalette(S.paletteIndex); }
    else if (e.key === 'Escape') { e.preventDefault(); closePalette(); }
  });
  $('#pList').addEventListener('click', (e) => {
    const b = e.target.closest('[data-pi]');
    if (b) runPalette(Number(b.dataset.pi));
  });

  document.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      S.paletteOpen ? closePalette() : openPalette();
      return;
    }
    if (mod && e.key === '0') {
      e.preventDefault();
      switchView('hud');
      return;
    }
    if (mod && ['1', '2', '3', '4', '5'].includes(e.key)) {
      e.preventDefault();
      const v = ['listen', 'workbench', 'files', 'people', 'settings'][Number(e.key) - 1];
      switchView(v);
      return;
    }
    const tag = (e.target && e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || tag === 'select' ||
      (e.target && e.target.isContentEditable);
    if (!typing && !mod && S.view === 'listen' && S.stage === 'annotate') {
      if (e.key === '[') { e.preventDefault(); setPane('side', !S.panes.side); return; }
      if (e.key === ']') { e.preventDefault(); setPane('insp', !S.panes.insp); return; }
      if (e.key === '\\') { e.preventDefault(); setPane('tx', !S.panes.tx); return; }
      if (e.key === 'm' || e.key === 'M' || e.key === 'b' || e.key === 'B') {
        e.preventDefault(); markMoment(); return;
      }
      if (e.code === 'Space' || e.key === ' ') { e.preventDefault(); togglePause(); return; }
    }
    if (!typing && !mod && S.view === 'hud' && !S.finish &&
        !(e.target && e.target.closest && e.target.closest('button'))) {
      if (e.key === 'b' || e.key === 'B' || e.key === 'm' || e.key === 'M') {
        e.preventDefault(); markMoment(); return;
      }
      if (e.code === 'Space' || e.key === ' ') { e.preventDefault(); togglePause(); return; }
    }
    if (e.key === 'Escape') {
      if (S.spkPop) { closeSpeakerPop(); return; }
      if (S.paletteOpen) { closePalette(); return; }
      if (S.modalOpen) { closeModal(); return; }
      if (S.addOpen) { closeAdd(); return; }
    }
  });

  window.addEventListener('message', (e) => {
    if (ENGINE_ORIGIN && e.origin !== ENGINE_ORIGIN) return;
    const d = e.data || {};
    if (d.type === 'ln-key' && d.key === 'k') { e.preventDefault?.(); S.paletteOpen ? closePalette() : openPalette(); }
    else if (d.type === 'ln-key' && ['1', '2', '3', '4', '5'].includes(String(d.key))) {
      switchView(['listen', 'workbench', 'files', 'people', 'settings'][Number(d.key) - 1]);
    } else if (d.type === 'ln-navigate' && d.href) {
      navigateToUrl(d.href);
    } else if (d.type === 'ln-session-changed' && d.session) {
      // 工作台内部换了场次：侧栏跟着选中，但不再重载 iframe。
      S.selectedSession = String(d.session);
      S.workbenchSid = String(d.session);
      if (S.view === 'workbench') renderSidebar();
    } else if (d.type === 'ln-ready') {
      if (S.view === 'workbench') {
        S.workbenchReady = true;
        if (d.session) {
          S.selectedSession = String(d.session);
          S.workbenchSid = String(d.session);
          renderSidebar();
        }
      }
    }
  });

  // 无边框窗口：自绘标题栏按钮。没有 Tauri（网页预览）时整组隐藏。
  const win = (TAURI && TAURI.window && TAURI.window.getCurrentWindow)
    ? TAURI.window.getCurrentWindow() : null;
  if (!win) {
    const box = $('.win-controls');
    if (box) box.hidden = true;
  } else {
    const on = (sel, fn) => { const b = $(sel); if (b) b.addEventListener('click', fn); };
    on('#winMin', () => win.minimize());
    on('#winMax', () => win.toggleMaximize());
    // 关闭按钮走壳内确认：有录音时先问，没录音按设置走托盘或退出。
    on('#winClose', () => requestClose());
    const bar = $('.topbar');
    if (bar) bar.addEventListener('dblclick', (e) => {
      if (e.target.closest('button,input,.cmdk')) return;
      if (e.target.closest('[data-tauri-drag-region]')) win.toggleMaximize();
    });
  }

  window.addEventListener('resize', () => { resizeScope(); applyAnnotLayout(); scheduleStageLayout(); });
  if (window.ResizeObserver) {
    new ResizeObserver(() => { resizeScope(); applyAnnotLayout(); scheduleStageLayout(); }).observe($('#stageWrap'));
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { resizeScope(); refreshRooms(); pollNotifications(); }
  });

  if (TAURI && TAURI.event && TAURI.event.listen) {
    TAURI.event.listen('shell_navigate', (e) => {
      if (e && e.payload) navigateToUrl(String(e.payload));
    });
    TAURI.event.listen('shell_show_listen', () => switchView('listen'));
    TAURI.event.listen('shell_close_requested', () => {
      invoke('shell_close_ack').catch(() => {});
      requestClose();
    });
    TAURI.event.listen('shell_quit_requested', (e) => {
      requestQuit(Number(e && e.payload) || 0);
    });
    TAURI.event.listen('shell_stop_all', () => stopAll());
    TAURI.event.listen('shell_tray_action', (e) => {
      let p = e && e.payload;
      if (typeof p === 'string') {
        try { p = JSON.parse(p); } catch (err) { p = { action: p }; }
      }
      handleTrayAction((p && (p.action || p.act)) || '');
    });
    // 系统通知点击：Rust 侧 on_action 只负责把 id 发回来，路由仍在壳里，
    // 这样通知中心和 toast 的行为完全一致。
    TAURI.event.listen('shell_notification_action', (e) => {
      routeSystemNotification(e && e.payload);
    });
  }
}

/* ================================================================ init */
// 自动化验收用的最小控制面。只在桌面壳里存在，网页端不会加载这份脚本。
window.__LN_SHELL__ = {
  state() {
    return {
      view: S.view, stage: S.stage, online: S.online,
      rooms: S.rooms.length, live: liveRooms().length,
      selected: S.selectedId, pageOpen: S.pageOpen,
      lines: S.lines.length, sessions: S.sessions.length,
      speakers: annotSpeakers().length, claimed: annotStats().claimed,
      todo: annotStats().todo, annotTab: S.annotTab, annotAuto: S.annotAuto,
      selectedSession: S.selectedSession, workbenchSid: S.workbenchSid,
      workbenchReady: S.workbenchReady, frameLoads: S.frameLoads,
      theme: resolvedTheme(), themeChoice, wbLines: S.wb.rows.length,
      wbSummaryScope: S.wb.summaryScope,
      trashCount: S.trashCount, batchMode: S.batchMode,
      batchSelected: S.batchSel.size,
      hudSid: S.hudSid, finish: !!S.finish,
      finishDone: !!(S.finish && S.finish.done),
      notifUnread: S.notif.unread, notifOpen: S.notif.open,
      notifItems: S.notif.items.length,
      notifLastPresent: S.notif.lastPresent, notifLastOs: S.notif.lastOs,
    };
  },
  report(tag) {
    const detail = JSON.stringify(this.state());
    return invoke('spike_report', { tag: tag || 'shell', detail })
      .catch(() => null);
  },
  view(name) { switchView(name); return name; },
  listen() { switchView('listen'); },
  select(i) {
    const r = S.rooms[i];
    if (r) selectRoom(r.id);
    return r ? r.id : null;
  },
  wall() { switchView('listen'); setStage('wall'); },
  signal() { switchView('listen'); setStage('signal'); },
  annotate() { switchView('listen'); return setStage('annotate'); },
  stage(mode) { return setStage(mode); },
  speakers() { return annotSpeakers(); },
  palette() { openPalette(); },
  paletteClose() { closePalette(); },
  page() { return openStagePage(); },
  pageClose() { return closeStageView(); },
  pageUrl(url) {
    const r = selectedRoom();
    if (r) { r.url = url; r.resolved_url = ''; renderStage(); }
    return r ? r.id : null;
  },
  add() { switchView('listen'); openAdd(); },
  filter(f) { S.filter = f; renderSidebar(); },
  stopModal() { $('#btnStop').click(); },
  stopModalClose() { closeModal(); },
  requestClose() { requestClose(); },
  closeAction() { return S.closeAction; },
  workbench(sid) { switchView('workbench', { session: sid || S.selectedSession }); },
  live(sid) { switchView('live', { session: sid || '', back: 'workbench' }); },
  frame(url, title) { switchView('frame', { url, title: title || '页面', back: 'workbench' }); },
};
/* ================================================================ 面板布局
   标注工作区按工作台式面板组织：房间列表、直播画面、实时字幕、本场声音
   各自可开可关；画面与字幕之间可拖高，任一可放大。原生子视图只跟着
   「直播画面」这块矩形走，关掉或放大字幕时直接隐藏。 */
const PANE_KEY = 'ln.shell.panes.v1';

function loadPanes() {
  try {
    const v = JSON.parse(localStorage.getItem(PANE_KEY) || 'null');
    if (!v) return;
    if (v.panes) Object.assign(S.panes, v.panes);
    if (typeof v.liveH === 'number') S.annotLiveH = clamp(v.liveH, 0.24, 0.82);
    if (typeof v.max === 'string') S.annotMax = v.max;
  } catch (e) {}
}
function savePanes() {
  try {
    localStorage.setItem(PANE_KEY, JSON.stringify({
      panes: S.panes, liveH: S.annotLiveH, max: S.annotMax,
    }));
  } catch (e) {}
}
function applyPaneButtons() {
  const body = $('#body');
  // 房间列表 / 本场声音的开关状态以实际布局为准：别的视图可能本来就收起。
  const actual = {
    side: !body.classList.contains('no-side'),
    insp: !body.classList.contains('no-insp'),
    live: S.panes.live,
    tx: S.panes.tx,
  };
  $$('#paneTools [data-pane]').forEach((b) => {
    const on = !!actual[b.dataset.pane];
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  });
}
function applyBodyPanes() {
  if (S.view !== 'listen') return;
  const body = $('#body');
  body.classList.toggle('no-side', !S.panes.side);
  body.classList.toggle('no-insp', !S.panes.insp);
}
/* 画面这块当前该不该露出来：位置模式、面板开关、放大状态、弹层，四者取与。 */
function stageWantsVisible() {
  const layoutOk = S.stage === 'page'
    ? true
    : (S.stage === 'annotate' && S.panes.live && S.annotMax !== 'tx');
  return layoutOk && S.overlayDepth === 0;
}
async function syncStageVisible() {
  if (!S.stageLabel) return;
  const want = stageWantsVisible();
  if (want === !S.pageHidden) return;
  S.pageHidden = !want;
  try { await invoke('room_show', { label: S.stageLabel, visible: want }); } catch (e) {}
  if (want) layoutStageView();
}
function applyAnnotLayout() {
  const annot = $('#annot');
  if (!annot || annot.hidden) return;
  const h = annot.clientHeight || 0;
  const liveOn = S.panes.live && S.annotMax !== 'tx';
  const txOn = S.panes.tx && S.annotMax !== 'live';
  const split = liveOn && txOn && !S.annotMax;
  const liveRow = !liveOn ? '0px'
    : S.annotMax === 'live' ? 'minmax(0,1fr)'
    : Math.round(h * S.annotLiveH) + 'px';
  annot.style.gridTemplateRows = liveRow + ' ' + (split ? '5px' : '0px') +
    ' ' + (txOn ? 'minmax(0,1fr)' : '0px');
  annot.dataset.live = liveOn ? 'on' : 'off';
  annot.dataset.tx = txOn ? 'on' : 'off';
  const empty = $('#annotEmpty');
  if (empty) empty.hidden = liveOn || txOn;
  $$('.apane-btn[data-pane-act="max"]').forEach((b) => {
    const on = S.annotMax === b.dataset.pane;
    b.classList.toggle('on', on);
    b.title = (on ? '还原' : '放大') + (b.dataset.pane === 'live' ? '画面' : '字幕');
  });
  syncStageVisible();
}
function setPane(k, on) {
  if (!(k in S.panes)) return;
  // 在别的页面点面板开关，先回到收听工作区再改。
  if (S.view !== 'listen' && (k === 'side' || k === 'insp')) switchView('listen');
  S.panes[k] = !!on;
  if (!on && S.annotMax === k) S.annotMax = '';
  applyBodyPanes();
  applyPaneButtons();
  applyAnnotLayout();
  savePanes();
  if (S.stage === 'annotate' && S.panes.live && S.annotMax !== 'tx' && !S.stageLabel) ensureAnnotPage();
}
function toggleAnnotMax(k) {
  if (k !== 'live' && k !== 'tx') return;
  S.annotMax = S.annotMax === k ? '' : k;
  if (S.annotMax) S.panes[k] = true;
  applyPaneButtons();
  applyAnnotLayout();
  savePanes();
  if (S.stage === 'annotate' && S.panes.live && S.annotMax !== 'tx' && !S.stageLabel) ensureAnnotPage();
}
function initPanes() {
  loadPanes();
  applyBodyPanes();
  applyPaneButtons();
  $$('#paneTools [data-pane]').forEach((b) => {
    b.addEventListener('click', () => setPane(b.dataset.pane, !S.panes[b.dataset.pane]));
  });
  const sideClose = $('#btnSideClose');
  if (sideClose) sideClose.addEventListener('click', () => setPane('side', false));
  const inspClose = $('#btnInspClose');
  if (inspClose) inspClose.addEventListener('click', () => setPane('insp', false));
  $$('.apane-btn[data-pane-act="max"]').forEach((b) => {
    b.addEventListener('click', () => toggleAnnotMax(b.dataset.pane));
  });
  $$('.apane-btn[data-pane-act="close"]').forEach((b) => {
    b.addEventListener('click', () => setPane(b.dataset.pane, false));
  });

  const split = $('#asplit');
  if (split) {
    split.addEventListener('pointerdown', (e) => {
      const annot = $('#annot');
      if (!annot || S.annotMax) return;
      const total = annot.clientHeight || 1;
      const startY = e.clientY;
      const startH = total * S.annotLiveH;
      let raf = 0;
      e.preventDefault();
      document.body.classList.add('splitting');
      const move = (ev) => {
        const h = clamp(startH + ev.clientY - startY, 150, Math.max(180, total - 170));
        S.annotLiveH = h / total;
        if (raf) return;
        raf = requestAnimationFrame(() => {
          raf = 0;
          annot.style.gridTemplateRows = Math.round(h) + 'px 5px minmax(0,1fr)';
          layoutStageView();
        });
      };
      const up = () => {
        document.body.classList.remove('splitting');
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        savePanes();
        layoutStageView();
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    });
    split.addEventListener('dblclick', () => {
      S.annotLiveH = 0.52;
      applyAnnotLayout();
      layoutStageView();
      savePanes();
    });
  }
}

function init() {
  initContextMenu();
  initPanes();
  bind();
  resizeScope();
  startScope();
  renderAll();
  switchView('listen');
  loadPerf();
  refreshRooms();
  fetchCloseAction();
  loadSessions();
  setTimeout(pollRoomsLoop, 300);
  setTimeout(pollTranscriptLoop, 900);
  setTimeout(pollSessionsLoop, 15000);
  setInterval(hudTick, 1000);
  setTimeout(pollNotifications, 1400);
}
init();

/* Parity UI -- vanilla JS, no build step, talks to the local API. */
'use strict';

const $ = (sel) => document.querySelector(sel);

/*
 * One <audio> element for the whole page, so previewing never overlaps itself:
 * pressing play anywhere moves playback there. The control lives in the topbar
 * and carries the transport, the position and the jump-to-cue buttons, which is
 * what you use to decide where a track fits and where its energy actually is.
 */
const player = {
  el: new Audio(),
  track: null,      // the track being previewed, or null
  pending: false,   // play requested, still waiting on the file
  error: false,
  missing: false,   // this track has no local file (streaming stub)
  cues: [],         // the previewed track's hot cues, as jump targets
  bpm: 0,
  timer: null,
  // A loop belongs to the preview: set in/out at the playhead, switch it on.
  loop: { on: false, start: null, end: null },
  // Tracks to play after this one, in order.  Client-side: it is a listening
  // queue, not part of the set.
  queue: [],
  beatJump: 4
};

const state = {
  meta: null,
  playlists: [],
  playlistId: null,
  playlistName: 'All tracks',
  pool: [],                 // enriched tracks of the current playlist
  trackIndex: new Map(),    // key -> enriched track
  set: [],                  // ordered array of enriched tracks
  lockedKeys: new Set(),
  excluded: new Set(),
  template: 'journey',
  curvePoints: null,        // [[x, y], ...] custom curve; null = use template
  values: {},               // last build stats from the server
  warnings: [],
  history: [],
  search: '',
  sort: 'title',
  localOnly: false,        // hide streaming-only tracks from the pool
  cuesOnly: false,         // show only tracks with generated cues
  folder: '',              // limit the pool to one folder on disk ("" = all)
  rx: null,                // direct-master.db status from /api/rekordbox
  writeStatus: null,       // whether the direct writers can run right now
  cuePolicy: 'overwrite',  // "overwrite" | "keep" for tracks that already have cues
  loadedSetId: null,       // the saved set the name box and Delete button refer to
  manage: null,            // the playlist listing behind the Manage panel
  ingest: null,            // ffmpeg status and last-used folders for rename & convert
  ingestPlan: null,        // the last folder scan, kept so Convert re-shows it
  watch: null,             // watch-folder settings and last scan
  pending: null,           // a dry run waiting for its commit {route, payload, label}
  metaKey: null,           // the track the metadata panel is editing
  preflight: null,         // the last pre-flight check (missing files, duplicates)
  duplicates: null,        // the last duplicate scan {scope, groups, ...}
  setTags: {},             // track key -> its set tag ([E7] [peak] 14/24), from /api/tags
  tagToken: 0,             // drops a tag answer that arrives after a newer set was built
  tagTimer: null,          // debounces the tag request to one per settled set
  dragging: null,
  poolRows: []
};

/* ------------------------------------------------------------------ api */

async function api(path, body) {
  const options = body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text.slice(0, 300) }; }
  if (!response.ok || data.error) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

let toastTimer = null;
function toast(message, isError = false) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.toggle('error', !!isError);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, isError ? 6000 : 3200);
}

/* --------------------------------------------------- camelot (JS mirror) */

function wheelDistance(a, b) {
  const na = parseInt(a, 10), nb = parseInt(b, 10);
  if (Number.isNaN(na) || Number.isNaN(nb)) return 99;
  const diff = Math.abs(na - nb);
  return Math.min(diff, 12 - diff);
}

function compatLabel(a, b) {
  if (!a || !b) return 'unknown';
  if (a === b) return 'perfect';
  const distance = wheelDistance(a, b);
  const sameMode = a.slice(-1) === b.slice(-1);
  if (distance === 0) return 'relative';
  if (distance === 1 && sameMode) return 'adjacent';
  if (distance === 1) return 'diagonal';
  if (distance === 2) return 'two-step';
  return 'clash';
}

function compatScore(a, b) {
  if (!a || !b) return 0.5;
  if (a === b) return 1;
  const distance = wheelDistance(a, b);
  const sameMode = a.slice(-1) === b.slice(-1);
  if (distance === 0) return 0.95;
  if (distance === 1 && sameMode) return 0.9;
  if (distance === 1) return 0.75;
  if (distance === 2 && sameMode) return 0.5;
  if (distance === 2) return 0.35;
  return 0;
}

function bpmDelta(a, b) {
  if (!a || !b) return 0;
  return Math.min(Math.abs(a - b), Math.abs(a - b * 2), Math.abs(a - b / 2));
}

/* ------------------------------------------------------------- helpers */

function curveFromPoints(points, length) {
  if (!points || !points.length) return new Array(length).fill(5);
  const ordered = points.map((p) => [Number(p[0]), Number(p[1])]).sort((a, b) => a[0] - b[0]);
  if (length === 0) return [];
  if (length === 1) return [ordered[0][1]];
  const out = [];
  for (let i = 0; i < length; i += 1) {
    const position = i / (length - 1);
    out.push(interpolate(ordered, position));
  }
  return out;
}

function interpolate(points, position) {
  if (position <= points[0][0]) return points[0][1];
  for (let i = 0; i < points.length - 1; i += 1) {
    const [x0, y0] = points[i];
    const [x1, y1] = points[i + 1];
    if (x1 <= position) continue;
    if (x1 === x0) return y1;
    return y0 + (y1 - y0) * ((position - x0) / (x1 - x0));
  }
  return points[points.length - 1][1];
}

function templatePoints() {
  const templates = (state.meta && state.meta.templates) || {};
  return (templates[state.template] || templates.journey || [[0, 5], [1, 5]]).map((p) => [p[0], p[1]]);
}

function activeCurvePoints() {
  return state.curvePoints || templatePoints();
}

function targets() {
  return curveFromPoints(activeCurvePoints(), state.set.length);
}

function fmtDuration(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}h ${m}m`;
  return m ? `${m}m ${s}s` : `${s}s`;
}

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function trackLabel(track) {
  if (track.artist) return `${track.artist} – ${track.title}`;
  return track.title || track.path || track.key;
}

function fmtClock(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

/* ---------------------------------------------------------------- preview */

function canPreview(track) {
  return !!track && !track.streaming && !!track.path;
}

function previewIcon(track) {
  if (!canPreview(track)) return '✕';
  return player.track && player.track.key === track.key ? '■' : '▶';
}

function previewTitle(track) {
  if (!canPreview(track)) return 'Streaming only — no local file to preview';
  return player.track && player.track.key === track.key
    ? 'Stop the preview'
    : 'Preview this track (Space toggles play/pause, [ and ] jump cue to cue)';
}

/*
 * A track that will not preview right now, and why. The pool holds tracks from
 * every source, so the control says so instead of failing on click. The first
 * two are about the file, which we cannot know without asking the filesystem.
 */
function previewBlocked(track) {
  if (!track) return 'That track is not in the loaded library.';
  if (track.streaming || !track.path) {
    return 'That track is streaming only - there is no local file to preview.';
  }
  if (player.missing && player.track && player.track.key === track.key) {
    return `No file at ${track.path}`;
  }
  return '';
}

function togglePreview(track) {
  if (!track) return;
  if (player.track && player.track.key === track.key) return stopPreview();
  const blocked = previewBlocked(track);
  if (blocked) { toast(blocked, true); return; }
  startPreview(track);
}

function startPreview(track) {
  player.track = track;
  player.pending = true;
  player.seekOnLoad = null;
  player.error = false;
  player.missing = false;
  player.cues = [];
  player.bpm = track.bpm || 0;
  player.loop = { on: false, start: null, end: null };
  player.el.src = `/api/audio?key=${encodeURIComponent(track.key)}`;
  player.el.play().catch(() => {});
  renderPlayer();
  renderPool();
  renderSet();
  loadCueMarkers(track);
}

function stopPreview() {
  player.el.pause();
  player.el.removeAttribute('src');
  try { player.el.load(); } catch { /* nothing to unload */ }
  player.track = null;
  player.pending = false;
  player.error = false;
  player.seekOnLoad = null;
  player.cues = [];
  renderPlayer();
  renderPool();
  renderSet();
}

/*
 * The last point in the file the browser will seek to, in seconds, or null when
 * it does not know yet.
 *
 * The cue canvas is laid out over the track's *analysed* length, which can be a
 * shade longer than the audio itself.  Seeking to it lands on the end and the
 * track stops immediately, so every seek is clamped to just inside the file.
 */
function seekLimit() {
  const duration = player.el.duration;
  return Number.isFinite(duration) && duration > 0 ? duration - 0.05 : null;
}

function seekTo(seconds) {
  if (!player.track) return;
  const limit = seekLimit();
  const target = limit === null ? seconds : Math.min(seconds, limit);
  try { player.el.currentTime = Math.max(0, target); } catch { return; }
  if (player.el.paused) player.el.play().catch(() => {});
  renderPlayer();
}

function nudge(seconds) {
  seekTo(Math.max(0, (player.el.currentTime || 0) + seconds));
}

async function loadCueMarkers(track) {
  try {
    const plan = await api(`/api/cues?key=${encodeURIComponent(track.key)}`);
    if (!player.track || player.track.key !== track.key) return;
    player.cues = plan.hot_cues || [];
    renderPlayer();
  } catch { /* the transport works without cue markers */ }
}

function cueButton(cue) {
  const label = cue.slot || cue.role || '?'; // A–H: one jump button per pad
  return `<button class="cue-jump c-${label.toLowerCase()}" data-cue="${cue.seconds}"
            title="${escapeHtml(cue.name || cue.role || '')} at ${cue.seconds}s">${label}</button>`;
}

function renderPlayer() {
  const node = $('#preview');
  const track = player.track;
  node.hidden = !track;
  if (!track) return;

  const duration = Number.isFinite(player.el.duration) ? player.el.duration : (track.duration || 0);
  const playing = !player.el.paused && !player.pending;
  const position = player.el.currentTime || 0;
  const percent = duration ? Math.min(100, (position / duration) * 100) : 0;
  const bpm = player.bpm ? `${player.bpm.toFixed(0)} BPM` : 'no BPM';

  $('#previewTitle').innerHTML = `<b>${escapeHtml(trackLabel(track))}</b>`
    + `<span class="muted"> · E${track.energy} · ${bpm} · ${escapeHtml(track.camelot || '–')}</span>`
    + (player.error ? '<span class="preview-error"> · could not play</span>'
      : player.pending ? '<span class="muted"> · loading…</span>' : '');
  $('#previewToggle').textContent = playing ? '⏸' : '▶';
  $('#previewToggle').title = playing ? 'Pause' : 'Play';
  $('#previewTime').textContent = `${fmtClock(position)} / ${fmtClock(duration)}`;
  $('#previewBar').style.width = `${percent}%`;
  const loop = player.loop || {};
  const hasLoop = loop.start !== null && loop.end !== null;
  const loopNode = $('#previewLoopState');
  if (loopNode) {
    loopNode.textContent = hasLoop ? `${fmtClock(loop.start)}–${fmtClock(loop.end)}` : '';
    loopNode.classList.toggle('on', Boolean(loop.on && hasLoop));
    loopNode.title = hasLoop
      ? (loop.on ? 'Loop on' : 'Loop off')
      : 'No loop set — ⇤ and ⇥ place its ends at the playhead';
  }
  const loopToggle = $('#loopToggle');
  if (loopToggle) loopToggle.classList.toggle('on', Boolean(loop.on && hasLoop));
  $('#previewCues').innerHTML = player.cues.length
    ? player.cues.map(cueButton).join('')
    : '<span class="muted">no cues yet</span>';
}

/* Loops and beat jump.  Both are in the player, not in the set: they are for
 * auditioning a track, and neither changes the cue plan or the export. */
function setLoopIn() {
  if (!player.track) return;
  const at = player.el.currentTime || 0;
  player.loop.start = at;
  if (player.loop.end !== null && player.loop.end <= at + 0.05) player.loop.end = null;
  renderPlayer();
}

function setLoopOut() {
  if (!player.track) return;
  const at = player.el.currentTime || 0;
  if (player.loop.start === null) player.loop.start = 0;
  if (at <= player.loop.start + 0.05) {
    toast('The loop out point has to be after the in point.', true);
    return;
  }
  player.loop.end = at;
  renderPlayer();
}

function toggleLoop() {
  if (!player.track) return;
  if (player.loop.start === null || player.loop.end === null) {
    // A loop with no ends is not a loop: make a two-bar one from here, so the
    // button does something useful on the first press.
    const bpm = player.bpm || (player.track && player.track.bpm) || 0;
    if (!bpm) { toast('Set the loop points with ⇤ and ⇥ first — this track has no BPM.', true); return; }
    const start = player.el.currentTime || 0;
    player.loop.start = start;
    player.loop.end = Math.min(start + (60 / bpm) * 8, Number.isFinite(player.el.duration) ? player.el.duration : start + 4);
  }
  player.loop.on = !player.loop.on;
  if (player.loop.on && player.el.currentTime >= player.loop.end) {
    seekTo(player.loop.start);
  }
  renderPlayer();
}

function enforceLoop() {
  const loop = player.loop || {};
  if (!loop.on || loop.start === null || loop.end === null) return;
  if ((player.el.currentTime || 0) >= loop.end - 0.03) {
    try { player.el.currentTime = loop.start; } catch { /* file refuses to seek */ }
  }
}

function beatJumpSeconds() {
  const bpm = player.bpm || (player.track && player.track.bpm) || 0;
  return bpm ? (60 / bpm) * player.beatJump : 0;
}

function jumpBeats(direction) {
  if (!player.track) return;
  const amount = beatJumpSeconds();
  if (!amount) { toast('Beat jump needs a BPM — analyse the track first.', true); return; }
  nudge(direction * amount);
}

/* The listening queue: tracks to hear after this one.  It is not the set. */
function queueTrack(track) {
  if (!track) return;
  if (track.streaming || !track.path) {
    toast('That track is streaming only - there is no local file to play.', true);
    return;
  }
  if (!player.track) {
    // Nothing is playing: queueing the first track is the same as starting it.
    startPreview(track);
    return;
  }
  player.queue.push(track);
  renderQueue();
  toast(`Queued ${trackLabel(track)}${player.track ? ' after this track' : ''}.`);
}

function renderQueue() {
  const count = player.queue.length;
  $('#queueCount').textContent = count;
  const list = $('#queueList');
  if (!list) return;
  if (!count) {
    list.innerHTML = '<div class="queue-empty">Nothing queued.</div>';
    return;
  }
  list.innerHTML = player.queue.map((track, index) => (
    `<div class="queue-row" data-queue-index="${index}" title="Play this now">
      <span class="num">${index + 1}</span>
      <div class="tt"><div class="t">${escapeHtml(trackLabel(track))}</div>
        <div class="s">${track.bpm ? Number(track.bpm).toFixed(0) + ' BPM · ' : ''}${escapeHtml(track.camelot || '–')} · E${track.energy}</div></div>
      <button class="icon-btn" data-queue-remove="${index}" title="Remove from the queue">✕</button>
    </div>`
  )).join('');
}

function playNextQueued() {
  if (!player.queue.length) {
    toast('The queue is empty — press ⏭ on a row to add one.', true);
    return;
  }
  const next = player.queue.shift();
  renderQueue();
  startPreview(next);
}

function clearQueue() {
  player.queue = [];
  renderQueue();
}

function previewPlayPause() {
  if (!player.track) return;
  if (player.el.paused) player.el.play().catch(() => {});
  else player.el.pause();
  renderPlayer();
}

function previewNextCue() {
  const position = player.el.currentTime || 0;
  const next = player.cues
    .map((cue) => cue.seconds)
    .filter((seconds) => seconds > position + 0.3)
    .sort((a, b) => a - b)[0];
  if (next === undefined) return;
  seekTo(next);
}

function previewPreviousCue() {
  const position = player.el.currentTime || 0;
  const previous = player.cues
    .map((cue) => cue.seconds)
    .filter((seconds) => seconds < position - 1.5)
    .sort((a, b) => b - a)[0];
  if (previous === undefined) return;
  seekTo(previous);
}

function bindPlayer() {
  const el = player.el;
  // Starting the preview from anywhere -- a pool row, a cue jump, a click on the
  // waveform -- has to wake the playhead, or the marker stands still while the
  // audio runs.
  el.addEventListener('play', () => { renderPlayer(); startCuePlayhead(); });
  el.addEventListener('pause', renderPlayer);
  el.addEventListener('ended', () => {
    if (player.queue.length) playNextQueued();
    else renderPlayer();
  });
  el.addEventListener('timeupdate', () => {
    enforceLoop();
    renderPlayer();
  });
  el.addEventListener('loadedmetadata', () => {
    player.pending = false;
    // A click on the waveform on a track that was not playing starts the
    // preview and lands on the click once the file knows how long it is.
    if (player.seekOnLoad !== null && player.seekOnLoad !== undefined) {
      // The file's length is known now, so a click that named a point past the
      // end lands just inside it instead of stopping the track on arrival.
      const limit = seekLimit();
      const target = player.seekOnLoad;
      player.seekOnLoad = null;
      try { el.currentTime = Math.max(0, limit === null ? target : Math.min(target, limit)); }
      catch { /* the file refuses to seek */ }
    }
    renderPlayer();
  });
  el.addEventListener('playing', () => { player.pending = false; player.error = false; renderPlayer(); });
  el.addEventListener('error', async () => {
    // A missing file and an undecodable format both land here. Ask the server
    // which it was, so the message names the real problem.
    const track = player.track;
    if (!track) return;
    player.pending = false;
    player.error = true;
    let missing = false;
    try {
      const response = await fetch(`/api/audio?key=${encodeURIComponent(track.key)}`, { method: 'HEAD' });
      missing = response.status === 404;
    } catch { /* the server is unreachable; report the format problem */ }
    if (!player.track || player.track.key !== track.key) return;
    player.missing = missing;
    renderPlayer();
    toast(previewBlocked(track) || 'That file could not be played in the browser.', true);
  });

  $('#previewToggle').addEventListener('click', previewPlayPause);
  $('#loopIn').addEventListener('click', setLoopIn);
  $('#loopOut').addEventListener('click', setLoopOut);
  $('#loopToggle').addEventListener('click', toggleLoop);
  $('#jumpBack').addEventListener('click', () => jumpBeats(-1));
  $('#jumpFwd').addEventListener('click', () => jumpBeats(1));
  $('#beatJump').addEventListener('change', (event) => {
    player.beatJump = Number(event.target.value) || 4;
  });
  $('#previewNext').addEventListener('click', playNextQueued);
  $('#queueBtn').addEventListener('click', (event) => {
    event.stopPropagation();
    $('#queueMenu').hidden = !$('#queueMenu').hidden;
    renderQueue();
  });
  $('#queueMenu').addEventListener('click', (event) => event.stopPropagation());
  document.addEventListener('click', () => { $('#queueMenu').hidden = true; });
  $('#queueList').addEventListener('click', (event) => {
    const remove = event.target.closest('[data-queue-remove]');
    if (remove) {
      player.queue.splice(Number(remove.getAttribute('data-queue-remove')), 1);
      renderQueue();
      return;
    }
    const row = event.target.closest('[data-queue-index]');
    if (!row) return;
    const next = player.queue.splice(Number(row.getAttribute('data-queue-index')), 1)[0];
    renderQueue();
    if (next) startPreview(next);
  });
  $('#queueClear').addEventListener('click', clearQueue);
  $('#previewCuesEdit').addEventListener('click', () => {
    if (player.track) openCueEditor(player.track);
  });
  $('#previewStop').addEventListener('click', stopPreview);
  $('#previewBack').addEventListener('click', () => nudge(-10));
  $('#previewFwd').addEventListener('click', () => nudge(10));
  $('#previewCues').addEventListener('click', (event) => {
    const button = event.target.closest('[data-cue]');
    if (button) seekTo(Number(button.getAttribute('data-cue')));
  });
}

const MAX_SET_LENGTH = 1000;

function setSetLength(value) {
  const ceiling = Number($('#lengthInput').max) || MAX_SET_LENGTH;
  const clamped = Math.max(2, Math.min(ceiling, Math.round(value || 20)));
  $('#lengthRange').value = clamped;
  $('#lengthInput').value = clamped;
  $('#lengthOut').textContent = clamped;
}

function currentConfig() {
  return {
    template: state.template,
    curve_points: state.curvePoints,
    length_tracks: Number($('#lengthInput').value) || 20,
    energy_weight: Number($('#energyWeight').value),
    key_weight: Number($('#keyWeight').value),
    bpm_weight: Number($('#bpmWeight').value),
    bpm_tolerance: Number($('#bpmTolerance').value),
    strictness: $('#strictnessSel').value,
    artist_repeat_window: Number($('#artistWindow').value),
    locks: buildLocks(),
    excluded: Array.from(state.excluded),
    seed: Math.floor(Math.random() * 1e9)
  };
}

function buildLocks() {
  const locks = {};
  state.set.forEach((track, index) => {
    if (state.lockedKeys.has(track.key)) locks[index] = track.key;
  });
  return locks;
}

function pushHistory() {
  state.history.push({ set: state.set.map((t) => t.key), locks: Array.from(state.lockedKeys) });
  if (state.history.length > 40) state.history.shift();
  $('#undoBtn').disabled = false;
}

function undo() {
  const snapshot = state.history.pop();
  if (!snapshot) return;
  state.set = snapshot.set.map((key) => state.trackIndex.get(key)).filter(Boolean);
  state.lockedKeys = new Set(snapshot.locks);
  renderSet();
  renderPool();
}

/* -------------------------------------------------------------- loading */

/*
 * Load a library from a rekordbox XML export.  There is no button for this any
 * more: Parity reads rekordbox's own database, so the toolbar was offering a
 * step nobody needs.  It stays reachable by dropping an export on the window,
 * because that is the only way in on a machine where the pyrekordbox extra is
 * not installed -- the database path needs it, and nothing else can load a
 * library at all.
 */
async function importFile(file) {
  if (/\.m3u8?$/i.test(file.name)) {
    await importM3UFile(file);
    return;
  }
  await importXml(file);
}

async function importXml(file) {
  if (!/\.xml$/i.test(file.name)) {
    toast(`${file.name} is not an XML export. A rekordbox collection XML `
      + `(File → Export Collection in xml format) loads the library.`, true);
    return;
  }
  try {
    const xml = await file.text();
    state.meta = await api('/api/import', { xml, source: file.name });
    await loadState();
    toast(`Imported ${state.meta.track_count} tracks from ${file.name}`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadState() {
  state.meta = await api('/api/state');
  try {
    state.rx = await api('/api/rekordbox');
  } catch {
    state.rx = null;
  }
  renderLibraryStatus();
  renderTemplateSelect();
  renderSavedSets();
  renderFolders();
  renderRekordboxStatus();
  renderCueStatus();
  renderAudioStatus();
  renderAnalysisStatus();
  state.cuePolicy = state.meta.cue_policy || 'overwrite';
  if ($('#writeCuePolicy')) $('#writeCuePolicy').value = state.cuePolicy;

  const loaded = !!state.meta.has_library;
  $('#reloadBtn').disabled = !loaded;
  $('#analyseBtn').disabled = !loaded;
  if ($('#audioEnergyBtn')) $('#audioEnergyBtn').disabled = !loaded;
  if ($('#analyseAudioBtn')) $('#analyseAudioBtn').disabled = !loaded;

  if (loaded) {
    await selectPlaylist(null, 'All tracks', true);
  }
  renderPlaylists();
}

// Streaming entries (SoundCloud etc.) are part of a rekordbox collection export
// but have no file behind them, so they are useful to see and easy to forget.
function analysisBadge(track) {
  const measured = track.analysis;
  if (!measured) return '';
  if (track.bpm_source !== 'analysed' && track.key_source !== 'analysed') return '';
  const parts = [];
  if (measured.bpm) parts.push(`${Number(measured.bpm).toFixed(2)} BPM`);
  if (measured.camelot || measured.key) parts.push(measured.camelot || measured.key);
  if (measured.first_beat_ms !== undefined) parts.push(`beat ${fmtClock(measured.first_beat_ms / 1000)}`);
  return `<span class="analysis-badge" title="measured from the audio: ${escapeHtml(parts.join(' · '))}${measured.note ? ' — ' + escapeHtml(measured.note) : ''}">audio</span>`;
}

function streamingLabel(track) {
  if (!track || !track.streaming) return '';
  const title = track.metadata_locked
    ? 'Streaming only — no local file. rekordbox stores this service’s track name encrypted, '
      + 'so Parity shows the service and track id instead.'
    : 'Streaming only — no local file';
  return ` · <span class="stream-badge" title="${title}">streaming</span>`;
}

/*
 * A generated-cue badge. The pipeline reads rekordbox's own phrase analysis
 * (Intro / Up / Down / Chorus / Outro) and places eight hot cues: A first
 * downbeat, B intro end, C build, D first drop, E breakdown, F second drop,
 * G outro, H mix-out. Anything without analysis is flagged as low confidence
 * rather than passed off as real.
 */
function cueBadge(track) {
  const cues = track && track.cues;
  if (!cues || !cues.analysed) return '';
  const low = cues.confidence !== 'high';
  const source = cues.source === 'rekordbox-anlz' ? 'rekordbox phrase analysis' : 'duration + BPM estimate';
  const locked = Number(cues.locked) || 0;
  const title = `${cues.hot} hot cues (A–H) and ${cues.memory} memory cues · ${source}`
    + (locked ? ` · ${locked} moved by hand and locked, which a re-analysis keeps` : '')
    + (low ? ' · low confidence, check in rekordbox' : '')
    + (cues.note ? ` · ${cues.note}` : '');
  return `<span class="cue-badge ${low ? 'low' : ''}" title="${escapeHtml(title)}">
    <span class="slots">A–H</span>${cues.hot}${locked ? ` 🔒${locked}` : ''}</span>`;
}

/*
 * Cues the track already carries in rekordbox. The two kinds are worth telling
 * apart: the eight pads Parity wrote last time are replaced on the next write,
 * while cues carrying a name the DJ typed are the ones the "keep" policy
 * protects. This badge is how you see which is which before a write.
 */
function existingCueBadge(track) {
  const existing = track && track.existing_cues;
  if (!existing || !existing.hot) return '';
  const details = (existing.user_cues || [])
    .map((cue) => `${cue.name || 'cue'} at ${Number(cue.seconds).toFixed(1)}s`)
    .join(', ');
  const title = existing.user
    ? `${existing.user} hot cue${existing.user === 1 ? '' : 's'} you placed${details ? ` (${details})` : ''}`
      + ` and ${existing.parity} written by Parity`
    : `${existing.hot} hot cues Parity wrote with an older cue layout — a write replaces them`;
  const label = existing.user ? 'yours' : 'rk';
  return `<span class="cue-badge existing ${existing.user ? 'yours' : ''}" title="${escapeHtml(title)}">`
    + `<span class="slots">${label}</span>${existing.user || existing.hot}</span>`;
}

function allCueBadges(track) {
  return cueBadge(track) + existingCueBadge(track);
}

/*
 * Per-track override for a track that already has cues: "keep" leaves the cues
 * on that one track alone, "write" replaces them, and the global setting covers
 * everything else.  Only tracks with cues of their own get the button.
 */
function cuePolicyButton(track) {
  const existing = track && track.existing_cues;
  if (!existing || !existing.hot) return '';
  const policy = track.cue_policy === 'keep' ? 'keep' : 'overwrite';
  const title = policy === 'keep'
    ? `Keeping this track's own cues — a write reorders it but does not touch its cue points. Click to replace them instead.`
    : `A write replaces this track's hot cues A–H. Click to keep the cues already on it instead.`;
  return `<button class="btn tiny ghost cue-policy-btn ${policy === 'keep' ? 'on' : ''}"
      data-act="cue-policy" title="${escapeHtml(title)}">${policy === 'keep' ? 'keep cues' : 'write cues'}</button>`;
}

async function setCuePolicy(payload) {
  try {
    const saved = await api('/api/cue-policy', payload);
    if (state.meta && saved.state) state.meta = saved.state;
    state.cuePolicy = (state.meta && state.meta.cue_policy) || 'overwrite';
    await refreshTracks();
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

/*
 * The track views carry the policy, so both panes are re-read after a change:
 * the badges, the per-track switches and the write menu's count all come from
 * the same place and must not disagree.
 */
async function refreshTracks() {
  try {
    const query = state.playlistId ? `?playlist=${encodeURIComponent(state.playlistId)}` : '';
    const data = await api(`/api/tracks${query}`);
    state.pool = data.tracks || [];
    state.pool.forEach((track) => state.trackIndex.set(track.key, track));
    state.set = state.set.map((track) => state.trackIndex.get(track.key) || track);
  } catch { /* keep what we have rather than blanking the panes */ }
  renderPool();
  renderSet();
  renderWriteCueNote();
}

function sourceName(source) {
  if (!source || source === 'stored' || source === 'upload') return '';
  return String(source).split('/').pop();
}

function renderLibraryStatus() {
  const meta = state.meta || {};
  const status = $('#libraryStatus');
  const meta2 = $('#libraryMeta');
  if (!meta.has_library) {
    status.textContent = 'No library loaded';
    if (meta2) meta2.textContent = '';
    return;
  }
  const overrides = meta.energy_overrides ? ` · ${meta.energy_overrides} energy edits` : '';
  status.textContent = `${meta.track_count} tracks · imported ${meta.imported_at || ''}${overrides}`;
  if (meta2) {
    const parts = [];
    if (meta.local_count) parts.push(`${meta.local_count} local`);
    if (meta.streaming_count) parts.push(`${meta.streaming_count} streaming`);
    if (meta.analysed_count) parts.push(`${meta.analysed_count} analysed`);
    const name = sourceName(meta.source);
    if (name && meta.source_kind !== 'db') parts.push(`from ${name}`);
    if (meta.source_kind === 'db') parts.push('from master.db');
    meta2.textContent = parts.join(' · ');
    meta2.title = meta.source || '';
  }
}

function renderFolders() {
  const select = $('#folderSel');
  const folders = (state.meta && state.meta.folders) || [];
  const options = ['<option value="">All folders</option>'];
  for (const entry of folders) {
    const short = entry.folder.split('/').filter(Boolean).slice(-2).join('/');
    const count = entry.local ? `${entry.local} local` : `${entry.total} tracks`;
    const selected = entry.folder === state.folder ? ' selected' : '';
    options.push(`<option value="${escapeHtml(entry.folder)}"${selected}>${escapeHtml(short)} — ${count}</option>`);
  }
  select.innerHTML = options.join('');
  select.disabled = folders.length === 0;
}

function renderRekordboxStatus() {
  const node = $('#rxSummary');
  const rx = state.rx;
  if (!rx) {
    node.hidden = true;
    return;
  }
  const extra = rx.pyrekordbox || {};
  const loaded = rx.library || {};
  const lines = [];
  if (!extra.available) {
    node.classList.add('warn');
    lines.push('<b>master.db support is off</b> — pyrekordbox is not installed in this interpreter.');
    lines.push(`<span class="muted">${escapeHtml(extra.hint || '')}</span>`);
  } else if (!rx.exists) {
    node.classList.add('warn');
    lines.push('<b>No rekordbox database found.</b>');
    lines.push(`<span class="rx-path">${escapeHtml(rx.path)}</span>`);
  } else {
    node.classList.remove('warn');
    lines.push(`<b>master.db</b> ready · pyrekordbox ${escapeHtml(extra.version || '?')}`
      + (rx.running ? ' · rekordbox is open' : ''));
    lines.push(`<span class="rx-path" title="${escapeHtml(rx.path)}">${escapeHtml(rx.path)}</span>`);
  }
  if (loaded.loaded) {
    lines.push(`Loaded: <b>${loaded.tracks}</b> tracks · ${loaded.local} local · ${loaded.streaming} streaming · ${loaded.analysed} analysed`);
  }
  node.innerHTML = lines.join('<br>');
  node.hidden = false;
}

function renderCueStatus() {
  const meta = state.meta || {};
  const node = $('#cueStatus');
  if (!meta.has_library) {
    node.textContent = '';
    return;
  }
  if (meta.stale_cue_plans) {
    // The cue layout changed since these plans were made (four cues then,
    // eight now), so the old ones were dropped rather than shown half-empty.
    node.textContent = `Cue layout changed — analyse again (${meta.stale_cue_plans} track`
      + `${meta.stale_cue_plans === 1 ? '' : 's'} out of date)`;
    return;
  }
  node.textContent = meta.cue_plan_count
    ? `${meta.cue_plan_count} of ${meta.track_count} tracks have cues`
    : 'No cues generated yet';
}

/* The filter the pool, a build and the pipeline all agree on. */
function currentFilter() {
  return {
    folder: state.folder || null,
    local_only: state.localOnly,
    require_file: true,
    dedupe: true
  };
}

function renderTemplateSelect() {
  const select = $('#templateSel');
  const templates = (state.meta && state.meta.templates) || {};
  const pretty = {
    warm_up: 'Warm up (3 → 7)',
    journey: 'Journey (up and back down)',
    peak_time: 'Peak time (6 → 9.5 → 7.5)',
    festival: 'Festival (5 → 10 → 6)',
    cool_down: 'Cool down (8 → 3)',
    chill: 'Chill (2.5 → 4 → 3)',
    flat: 'Flat (steady 6)',
    build_and_hold: 'Build and hold',
    opener_to_peak: 'Opener to peak'
  };
  select.innerHTML = Object.keys(templates)
    .map((name) => `<option value="${name}">${pretty[name] || name}</option>`)
    .join('') + '<option value="custom">Custom (dragged)</option>';
  select.value = state.template;
}

function renderSavedSets() {
  const select = $('#savedSets');
  const sets = (state.meta && state.meta.saved_sets) || [];
  select.innerHTML = '<option value="">Saved sets…</option>' +
    sets.map((s) => `<option value="${s.id}">${escapeHtml(s.name)} (${s.count})</option>`).join('');
  // The dropdown shows which saved set Delete acts on, so it has to follow the
  // loaded set rather than always falling back to the placeholder.
  select.value = state.loadedSetId && sets.some((s) => s.id === state.loadedSetId)
    ? state.loadedSetId
    : '';
}

function renderPlaylists() {
  const container = $('#playlistTree');
  const playlists = (state.meta && state.meta.playlists) || [];
  if (!playlists.length) {
    container.innerHTML = '<p class="empty">Import your rekordbox XML to get started.<br>' +
      'In rekordbox: <b>File → Export Collection in xml format</b>.</p>';
    return;
  }

  const parts = [];
  parts.push(`<div class="leaf ${state.playlistId === null ? 'active' : ''}" data-playlist="">
      <span>All tracks</span><span class="count">${state.meta.track_count}</span></div>`);

  const node = (item, depth) => {
    if (item.is_folder || item.children.length) {
      const open = depth < 1 ? 'open' : '';        return `<details ${open}><summary><span>${escapeHtml(item.name)}</span>
        <span class="count">${item.total}</span></summary>
        ${item.children.map((child) => node(child, depth + 1)).join('')}
      </details>`;
    }
    return `<div class="leaf ${state.playlistId === item.id ? 'active' : ''}" data-playlist="${item.id}">
      <span>${escapeHtml(item.name)}</span><span class="count">${item.count}</span></div>`;
  };
  playlists.forEach((item) => parts.push(node(item, 0)));
  container.innerHTML = parts.join('');

  container.querySelectorAll('[data-playlist]').forEach((element) => {
    element.addEventListener('click', (event) => {
      event.preventDefault();
      const id = element.getAttribute('data-playlist') || null;
      const name = element.querySelector('span') ? element.querySelector('span').textContent : 'All tracks';
      selectPlaylist(id, name);
    });
  });
}

async function selectPlaylist(id, name, silent = false) {
  state.playlistId = id;
  state.playlistName = name || 'All tracks';
  try {
    const query = id ? `?playlist=${encodeURIComponent(id)}` : '';
    const data = await api(`/api/tracks${query}`);
    state.pool = data.tracks || [];
    state.pool.forEach((track) => state.trackIndex.set(track.key, track));
    if (!silent) {
      toast(`${state.pool.length} tracks loaded from “${state.playlistName}”`);
    }
  } catch (error) {
    toast(error.message, true);
    return;
  }
  const ceiling = Math.max(2, Math.min(MAX_SET_LENGTH, state.pool.length));
  const range = $('#lengthRange');
  const number = $('#lengthInput');
  range.max = ceiling;
  number.max = ceiling;
  if (Number(number.value) > ceiling) setSetLength(ceiling);
  renderPlaylists();
  renderPool(true);
  renderCurve();
  renderSet();
}

/* ----------------------------------------------------------- pool table */

function sortedPool() {
  const query = state.search.trim().toLowerCase();
  let rows = state.pool;
  if (state.localOnly) rows = rows.filter((track) => !track.streaming);
  if (state.cuesOnly) rows = rows.filter((track) => track.cues && track.cues.analysed);
  if (state.folder) rows = rows.filter((track) => track.folder === state.folder);
  if (query) {
    rows = rows.filter((track) => [track.title, track.artist, track.genre, track.album, track.label]
      .some((field) => String(field || '').toLowerCase().includes(query)));
  }
  const key = (track) => {
    switch (state.sort) {
      case 'bpm': return [track.bpm || 0, track.label];
      case 'energy': return [track.energy || 0, track.label];
      case 'genre': return [String(track.genre || ''), track.label];
      case 'key': return [track.camelot ? parseInt(track.camelot, 10) : 99, track.camelot || '', track.label];
      default: return [String(track.label || '').toLowerCase()];
    }
  };
  return rows.slice().sort((a, b) => {
    const ka = key(a), kb = key(b);
    for (let i = 0; i < Math.max(ka.length, kb.length); i += 1) {
      const va = ka[i], vb = kb[i];
      if (va === vb) continue;
      return va > vb ? 1 : -1;
    }
    return 0;
  });
}

/*
 * The pool can hold thousands of tracks, so rows are virtualised: only the
 * slice near the viewport exists in the DOM, positioned inside a spacer that
 * carries the full scroll height. Rows are handled by delegation, which also
 * keeps working as the window slides.
 */
const ROW_H = 34; // must match .track-row height in style.css
const POOL_BUFFER = 10;
let poolWindow = [-1, -1];
let poolScrollFrame = null;

function renderPool(resetScroll = false) {
  const container = $('#trackList');
  if (resetScroll) container.scrollTop = 0;
  state.poolRows = sortedPool();
  poolWindow = [-1, -1];
  $('#poolCount').textContent = state.poolRows.length;

  if (!state.poolRows.length) {
    let message = 'Pick a playlist on the left, or import your collection.';
    if (state.pool.length) {
      message = state.localOnly
        ? 'No local files match — untick <b>Local files only</b> to include the streaming tracks.'
        : 'No tracks match that search.';
    }
    container.innerHTML = `<p class="empty">${message}</p>`;
    return;
  }

  if (!container.querySelector('.pool-spacer')) {
    container.innerHTML = '<div class="pool-spacer"></div>';
  }
  container.querySelector('.pool-spacer').style.height = `${state.poolRows.length * ROW_H}px`;
  renderPoolWindow();
}

/* Preview buttons work by delegation, which is what the virtualised pool needs. */
function previewButton(event, scope) {
  if (!event.target.closest('[data-act="preview"]')) return false;
  const row = event.target.closest(scope);
  const track = row && state.trackIndex.get(row.getAttribute('data-key'));
  if (track) togglePreview(track);
  return true;
}

function renderPoolWindow() {
  const rows = state.poolRows || [];
  const container = $('#trackList');
  if (!rows.length) return;
  const spacer = container.querySelector('.pool-spacer');
  if (!spacer) return;

  const first = Math.max(0, Math.floor(container.scrollTop / ROW_H) - POOL_BUFFER);
  const last = Math.min(
    rows.length,
    Math.ceil((container.scrollTop + container.clientHeight) / ROW_H) + POOL_BUFFER
  );
  if (first === poolWindow[0] && last === poolWindow[1]) return;
  poolWindow = [first, last];

  const inSet = new Set(state.set.map((t) => t.key));
  const parts = [];
  for (let index = first; index < last; index += 1) {
    parts.push(poolRowHtml(rows[index], index, inSet));
  }
  spacer.innerHTML = parts.join('');
}

/*
 * Where an energy value came from, in words.  A measured value is worth saying
 * so about: it is the difference between "loudness, brightness, range and drive
 * off the file" and "the genre and stars in its tags".
 */
function energyTooltip(track) {
  const audio = track.audio || {};
  if (track.energy_source === 'audio') {
    return `measured from the audio — loudness ${Number(audio.loudness_dbfs).toFixed(1)} dBFS, `
      + `brightness ${Math.round(audio.centroid_hz)} Hz, range ${Number(audio.dynamic_range_db).toFixed(1)} dB, `
      + `drive ${Number(audio.onset_rate).toFixed(1)}/s`;
  }
  const note = track.needs_review ? ' — estimate, check it' : '';
  return `${track.energy_source}${note}`;
}

function poolRowHtml(track, index, inSet) {
  const excluded = state.excluded.has(track.key);
  return `<div class="track-row ${inSet.has(track.key) ? 'in-set' : ''} ${excluded ? 'dim' : ''}"
       style="top:${index * ROW_H}px" data-index="${index}" data-key="${track.key}">
    <button class="add-btn" data-act="add" title="Add to the end of the set">+</button>
    <button class="play-btn ${canPreview(track) ? '' : 'off'}" data-act="preview" title="${previewTitle(track)}">${previewIcon(track)}</button>
    <button class="icon-btn ${canPreview(track) ? '' : 'off'}" data-act="cues" title="${canPreview(track) ? "Edit this track's cues on the waveform" : 'Streaming only — there is no local file to draw'}">〰</button>
    <button class="icon-btn" data-act="edit" title="Edit this track's metadata">✎</button>
    <button class="icon-btn" data-act="queue" title="Play this after the current track">⏭</button>
    <input type="checkbox" data-act="include" ${excluded ? '' : 'checked'} title="Include when building">
    <div class="tt">
      <div class="t">${escapeHtml(track.title || '')}</div>
      <div class="s">${escapeHtml(track.artist || '')}${track.genre ? ' · ' + escapeHtml(track.genre) : ''}${track.bpm ? '' : ' · no BPM'}${streamingLabel(track)}${analysisBadge(track)}${allCueBadges(track)}</div>
    </div>
    <div class="bpm" title="${track.bpm_source === 'analysed' ? 'measured from the audio' : ''}">${track.bpm ? track.bpm.toFixed(0) : '–'}</div>
    <div><span class="key-badge" title="${track.key_source === 'analysed' ? 'measured from the audio' : ''}">${escapeHtml(track.camelot || '–')}</span></div>
    <input class="energy-input ${track.energy_override ? 'overridden' : ''} ${track.needs_review ? 'review' : ''} ${track.energy_source === 'audio' ? 'measured' : ''}"
           type="number" min="1" max="10" step="0.5" value="${track.energy}"
           data-act="energy" title="${energyTooltip(track)}">
  </div>`;
}

async function setEnergy(track, value) {
  try {
    await api('/api/energy', { values: { [track.key]: value } });
  } catch (error) {
    toast(error.message, true);
    return;
  }
  const apply = (item) => {
    if (!item.energy_override && state.meta) {
      state.meta.energy_overrides = (state.meta.energy_overrides || 0) + 1;
    }
    item.energy = value;
    item.energy_override = true;
    item.energy_source = 'override';
    item.needs_review = false;
  };
  if (state.trackIndex.has(track.key)) apply(state.trackIndex.get(track.key));
  state.pool.forEach((item) => { if (item.key === track.key) apply(item); });
  state.set.forEach((item) => { if (item.key === track.key) apply(item); });
  renderPool();
  renderSet();
  renderLibraryStatus();
}

/* ------------------------------------------------------------ set table */

function computeStats() {
  const tracks = state.set;
  if (!tracks.length) return null;
  const energies = tracks.map((t) => Number(t.energy) || 0);
  const bpms = tracks.filter((t) => t.bpm).map((t) => t.bpm);
  let good = 0, beatmatched = 0, clashes = 0;
  const tolerance = Number($('#bpmTolerance').value);
  for (let i = 1; i < tracks.length; i += 1) {
    const a = tracks[i - 1], b = tracks[i];
    if (compatScore(a.camelot, b.camelot) >= 0.75) good += 1;
    if (bpmDelta(a.bpm, b.bpm) <= tolerance) beatmatched += 1;
    if (compatLabel(a.camelot, b.camelot) === 'clash') clashes += 1;
  }
  const transitions = Math.max(1, tracks.length - 1);
  // How closely the set follows the drawn target: the average distance of a
  // track from its point on the curve, and the worst single miss.
  const targetList = targets();
  let deviation = 0, worst = 0;
  tracks.forEach((track, index) => {
    const target = targetList[index] === undefined ? 5 : targetList[index];
    const distance = Math.abs((Number(track.energy) || 0) - target);
    deviation += distance;
    if (distance > worst) worst = distance;
  });
  return {
    count: tracks.length,
    curveFit: deviation / tracks.length,
    curveWorst: worst,
    duration: tracks.reduce((sum, t) => sum + (t.duration || 0), 0),
    avgEnergy: energies.reduce((a, b) => a + b, 0) / energies.length,
    peakEnergy: Math.max(...energies),
    bpmMin: bpms.length ? Math.min(...bpms) : 0,
    bpmMax: bpms.length ? Math.max(...bpms) : 0,
    harmonic: (good / transitions) * 100,
    beatmatched: (beatmatched / transitions) * 100,
    clashes,
    streaming: tracks.filter((t) => t.streaming).length
  };
}

function renderStats() {
  const stats = computeStats();
  const container = $('#stats');
  if (!stats) { container.innerHTML = '<span>Build a set to see its numbers.</span>'; return; }
  const harmonyColor = stats.harmonic >= 80 ? 'var(--good)' : stats.harmonic >= 50 ? 'var(--warn)' : 'var(--bad)';
  const fitColor = stats.curveWorst <= 1 ? 'var(--good)' : stats.curveWorst <= 2 ? 'var(--warn)' : 'var(--bad)';
  container.innerHTML = `
    <span>Tracks <b>${stats.count}</b></span>
    <span>Runtime <b>${fmtDuration(stats.duration)}</b></span>
    <span style="color:${fitColor}" title="Average and worst distance from the target energy curve">Curve <b>±${stats.curveFit.toFixed(2)}</b> (worst ${stats.curveWorst.toFixed(2)})</span>
    <span>Energy <b>${stats.avgEnergy.toFixed(1)}</b> avg · <b>${stats.peakEnergy.toFixed(1)}</b> peak</span>
    <span>BPM <b>${stats.bpmMin.toFixed(0)}–${stats.bpmMax.toFixed(0)}</b></span>
    <span style="color:${harmonyColor}">Harmonic <b>${stats.harmonic.toFixed(0)}%</b></span>
    <span>Beatmatch <b>${stats.beatmatched.toFixed(0)}%</b></span>
    ${stats.streaming ? `<span style="color:var(--warn)" title="These tracks have no local file — they only play from a streaming service">Streaming only <b>${stats.streaming}</b></span>` : ''}
    ${stats.clashes ? `<span style="color:var(--bad)">Key clashes <b>${stats.clashes}</b></span>` : ''}`;
}

/*
 * Says, in the write menu, how much of what is about to be written is already
 * there: how many of the set's tracks carry cues, and how many of those carry
 * cues the DJ placed rather than ones Parity wrote.
 */
function renderWriteCueNote() {
  const node = $('#writeCueNote');
  if (!node) return;
  const cued = state.set.filter((track) => track.existing_cues && track.existing_cues.hot);
  const theirs = cued.filter((track) => track.existing_cues.user);
  if (!state.set.length) {
    node.textContent = '';
    return;
  }
  const policy = state.cuePolicy === 'keep' ? 'keep' : 'overwrite';
  if (!cued.length) {
    node.textContent = `${state.set.length} tracks in this set have no cues yet.`;
    return;
  }
  const yours = theirs.length
    ? `${theirs.length} with cues you placed (${state.meta && state.meta.cue_override_count ? `${state.meta.cue_override_count} per-track choice${state.meta.cue_override_count === 1 ? '' : 's'}` : 'no per-track choices'})`
    : 'none of them your own';
  node.innerHTML = `${cued.length} of ${state.set.length} tracks already have hot cues — ${yours}. ` +
    (policy === 'keep'
      ? 'Those tracks keep their cues and are only reordered.'
      : 'A write replaces them with A–H.');
}

function renderSet() {
  const container = $('#setList');
  $('#setCount').textContent = state.set.length;
  $('#undoBtn').disabled = state.history.length === 0;
  renderStats();
  renderCurve();
  renderWriteCueNote();
  // Always, not just when the set is empty: the build's notes about the curve
  // are the whole point of them, and a built set is never empty.
  renderWarnings();

  if (!state.set.length) {
    container.innerHTML = '<p class="empty">Nothing here yet — hit <b>Build set</b>, or add tracks with <b>+</b> from the pool.</p>';
    return;
  }

  const parts = [];
  state.set.forEach((track, index) => {
    if (index > 0) {
      const previous = state.set[index - 1];
      const label = compatLabel(previous.camelot, track.camelot);
      const delta = bpmDelta(previous.bpm, track.bpm);
      const tolerance = Number($('#bpmTolerance').value);
      const description = {
        perfect: 'same key', relative: 'relative key', adjacent: 'one step on the wheel',
        diagonal: 'diagonal move', 'two-step': 'two steps (energy lift)', clash: 'key clash', unknown: 'key unknown'
      }[label] || label;
      const bpmText = previous.bpm && track.bpm
        ? ` · ${delta <= tolerance ? '' : '+'}${delta.toFixed(1)} BPM${delta > tolerance ? ' ⚠' : ''}`
        : '';
      parts.push(`<div class="transition-badge t-${label}" title="${description}">
        <span class="dotmark"></span><span>${description}${bpmText}</span></div>`);
    }

    const locked = state.lockedKeys.has(track.key);
    parts.push(`<div class="set-row ${locked ? 'locked' : ''}" draggable="${!locked}" data-index="${index}" data-key="${track.key}">
      <span class="idx">${index + 1}</span>
      <button class="play-btn ${canPreview(track) ? '' : 'off'}" data-act="preview" title="${previewTitle(track)}">${previewIcon(track)}</button>
      <button class="icon-btn ${locked ? 'on' : ''}" data-act="lock" title="${locked ? 'Unpin' : 'Pin to this position'}">${locked ? '🔒' : '🔓'}</button>
      <div class="tt">
        <div class="t">${escapeHtml(trackLabel(track))}</div>
        <div class="s">${track.bpm ? track.bpm.toFixed(1) + ' BPM' : 'no BPM'} · ${escapeHtml(track.camelot || track.tonality || '–')}${track.musical_key ? ` (${track.musical_key})` : ''} · E${track.energy} · ${escapeHtml(track.genre || '')}${streamingLabel(track)}${analysisBadge(track)}${allCueBadges(track)}</div>
      </div>
      <span class="set-tag" hidden></span>
      <div class="row-actions">
        <button class="icon-btn" data-act="queue" title="Play this after the current track">⏭</button>
        <button class="icon-btn" data-act="edit" title="Edit this track's metadata">✎</button>
        ${cuePolicyButton(track)}
        <button class="icon-btn" data-act="cues" title="Edit this track's cues on the waveform">〰</button>
        <button class="icon-btn" data-act="up" title="Move up">↑</button>
        <button class="icon-btn" data-act="down" title="Move down">↓</button>
        <button class="icon-btn" data-act="remove" title="Remove from set">✕</button>
      </div>
    </div>`);
  });

  container.innerHTML = parts.join('');
  container.querySelectorAll('.set-row').forEach((row) => bindSetRow(row));
  // Painted from the plan already in hand, then asked for again: reordering a
  // set changes every tag in it, and the arithmetic is the server's.
  paintSetTags();
  scheduleSetTags();
}

/*
 * The set tag, on the row it is about.
 *
 * The tag says three things -- the energy band, the part the track plays and
 * where it sits -- and it is derived from the set, so the server works it out:
 * it is the same code the export and the write run (see parity/tags.py), and
 * asking for it is what makes the tag on the row the tag in the file.
 *
 * One request per settled set.  A drag, a rebuild and a retyped energy all land
 * in the same window, and an answer for a set that has since changed is dropped
 * rather than painted over the new one.
 */
function scheduleSetTags(delay = 200) {
  clearTimeout(state.tagTimer);
  state.tagTimer = setTimeout(refreshSetTags, delay);
}

async function refreshSetTags() {
  const order = state.set.map((track) => track.key);
  const token = (state.tagToken += 1);
  if (!order.length) {
    state.setTags = {};
    paintSetTags();
    return;
  }
  let data;
  try {
    data = await api('/api/tags', { order });
  } catch {
    return; // the tag is a label on a row; the set works without one
  }
  if (token !== state.tagToken) return;
  state.setTags = {};
  for (const row of data.rows || []) state.setTags[row.key] = row;
  paintSetTags();
}

function paintSetTags() {
  // A track can be in the set twice, and then one comment cannot carry both
  // tags: the plan keeps the last place the track sits, and both rows show that
  // one tag.  Marking every row of such a track is better than a tag that
  // quietly disagrees with the position next to it (pre-flight flags the
  // duplicate as well).
  const repeats = new Map();
  for (const track of state.set) {
    if (track) repeats.set(track.key, (repeats.get(track.key) || 0) + 1);
  }
  document.querySelectorAll('#setList .set-row').forEach((node) => {
    const chip = node.querySelector('.set-tag');
    if (!chip) return;
    const key = node.getAttribute('data-key');
    const plan = state.setTags[key];
    const twice = (repeats.get(key) || 0) > 1;
    chip.textContent = plan ? plan.tag : '';
    chip.hidden = !plan;
    chip.classList.toggle('dup', twice);
    chip.title = plan
      ? (twice
        ? `${plan.tag} — this track is in the set twice, and one comment cannot hold two `
          + `positions, so the tag carries the place the file will get (${plan.position}/${plan.total})`
          + ` on both rows. Pre-flight flags the repeat.`
        : `${plan.tag} — ${plan.reason}. The comment becomes “${plan.after}” on export or write.`)
      : '';
  });
}

function bindSetRow(row) {
  const index = Number(row.getAttribute('data-index'));
  const track = state.set[index];
  if (!track) return;

  row.querySelector('[data-act="preview"]').addEventListener('click', () => togglePreview(track));
  const waveButton = row.querySelector('[data-act="cues"]');
  if (waveButton) waveButton.addEventListener('click', () => openCueEditor(track));
  const editButton = row.querySelector('[data-act="edit"]');
  if (editButton) editButton.addEventListener('click', () => openMetaEditor(track));
  const queueButton = row.querySelector('[data-act="queue"]');
  if (queueButton) queueButton.addEventListener('click', () => queueTrack(track));
  const policyButton = row.querySelector('[data-act="cue-policy"]');
  if (policyButton) {
    policyButton.addEventListener('click', async () => {
      const next = track.cue_policy === 'keep' ? 'overwrite' : 'keep';
      // Agreeing with the global setting needs no exception of its own, so the
      // switch drops back to "follow the setting" rather than storing a value
      // that matches it -- otherwise every track ever flipped keeps a line.
      const value = next === state.cuePolicy ? 'auto' : next;
      if (await setCuePolicy({ values: { [track.key]: value } })) {
        const label = state.trackIndex.get(track.key) || track;
        toast(next === 'keep'
          ? `Keeping the cues already on ${trackLabel(label)}.`
          : `A write will replace the cues on ${trackLabel(label)}.`);
      }
    });
  }
  row.querySelector('[data-act="lock"]').addEventListener('click', () => {
    pushHistory();
    if (state.lockedKeys.has(track.key)) state.lockedKeys.delete(track.key);
    else state.lockedKeys.add(track.key);
    renderSet();
  });
  row.querySelector('[data-act="remove"]').addEventListener('click', () => {
    pushHistory();
    state.set.splice(index, 1);
    renderSet();
    renderPool();
  });
  const move = (offset) => {
    const target = index + offset;
    if (target < 0 || target >= state.set.length) return;
    const targetTrack = state.set[target];
    if (state.lockedKeys.has(targetTrack.key)) {
      toast('That position is pinned — unpin it first.', true);
      return;
    }
    pushHistory();
    const [moved] = state.set.splice(index, 1);
    state.set.splice(target, 0, moved);
    renderSet();
    renderPool();
  };
  row.querySelector('[data-act="up"]').addEventListener('click', () => move(-1));
  row.querySelector('[data-act="down"]').addEventListener('click', () => move(1));

  row.addEventListener('dragstart', (event) => {
    state.dragging = index;
    row.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', String(index));
  });
  row.addEventListener('dragend', () => {
    state.dragging = null;
    row.classList.remove('dragging');
    document.querySelectorAll('.set-row.drop-target').forEach((el) => el.classList.remove('drop-target'));
  });
  row.addEventListener('dragover', (event) => {
    event.preventDefault();
    row.classList.add('drop-target');
  });
  row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
  row.addEventListener('drop', (event) => {
    event.preventDefault();
    row.classList.remove('drop-target');
    const from = state.dragging;
    if (from === null || from === undefined || from === index) return;
    const targetTrack = state.set[index];
    if (state.lockedKeys.has(targetTrack.key)) {
      toast('That position is pinned — unpin it first.', true);
      return;
    }
    pushHistory();
    const [moved] = state.set.splice(from, 1);
    state.set.splice(index, 0, moved);
    renderSet();
    renderPool();
  });
}

function renderWarnings() {
  const container = $('#warnings');
  container.innerHTML = (state.warnings || []).map((w) => `<div>⚠ ${escapeHtml(w)}</div>`).join('');
}

/* -------------------------------------------------------- curve editor */

const CHART = { width: 600, height: 190, left: 34, right: 10, top: 14, bottom: 24 };

function xFor(position) {
  return CHART.left + position * (CHART.width - CHART.left - CHART.right);
}
function yFor(energy) {
  const clamped = Math.max(1, Math.min(10, energy));
  return CHART.top + (1 - (clamped - 1) / 9) * (CHART.height - CHART.top - CHART.bottom);
}
function positionFromX(x) {
  return Math.max(0, Math.min(1, (x - CHART.left) / (CHART.width - CHART.left - CHART.right)));
}
function energyFromY(y) {
  const ratio = 1 - (y - CHART.top) / (CHART.height - CHART.top - CHART.bottom);
  return Math.max(1, Math.min(10, 1 + ratio * 9));
}

/*
 * The energy range the pool can actually reach. A curve that leaves this band
 * is a curve no track can follow, which is the usual reason a built set does
 * not look like the example -- so the band is drawn rather than left to the
 * user to work out from the numbers.
 */
function poolEnergyRange() {
  const rows = sortedPool().filter((track) => !state.excluded.has(track.key));
  const energies = rows.map((track) => Number(track.energy)).filter((value) => Number.isFinite(value));
  if (energies.length < 2) return null;
  return [Math.min(...energies), Math.max(...energies)];
}

function renderCurve() {
  const svg = $('#curveChart');
  const points = activeCurvePoints();
  const parts = [];

  const reachable = poolEnergyRange();
  if (reachable) {
    const top = yFor(reachable[1]);
    const bottom = yFor(reachable[0]);
    parts.push(`<rect class="pool-band" x="${CHART.left}" y="${top.toFixed(1)}" ` +
      `width="${CHART.width - CHART.left - CHART.right}" height="${Math.max(1, bottom - top).toFixed(1)}"></rect>`);
    parts.push(`<text class="pool-label" x="${CHART.left + 4}" y="${(top + 11).toFixed(1)}">` +
      `pool E${reachable[0].toFixed(1)}–E${reachable[1].toFixed(1)}</text>`);
  }

  for (let energy = 2; energy <= 10; energy += 2) {
    const y = yFor(energy);
    parts.push(`<line class="grid-line" x1="${CHART.left}" y1="${y}" x2="${CHART.width - CHART.right}" y2="${y}"></line>`);
    parts.push(`<text class="axis-label" x="4" y="${y + 3}">E${energy}</text>`);
  }

  const path = points.map(([x, y], i) => `${i ? 'L' : 'M'}${xFor(x).toFixed(1)},${yFor(y).toFixed(1)}`).join(' ');
  parts.push(`<path class="target-line" d="${path}"></path>`);

  if (state.set.length) {
    const targetList = targets();
    const actual = state.set.map((track, index) => {
      const x = state.set.length === 1 ? 0.5 : index / (state.set.length - 1);
      return [xFor(x), yFor(Number(track.energy) || 0)];
    });
    parts.push(`<path class="actual-line" d="${actual.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ')}"></path>`);
    actual.forEach(([x, y]) => parts.push(`<circle class="dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3"></circle>`));
    // Flag the biggest deviations between the curve and the built set.
    targetList.forEach((target, index) => {
      const energy = Number(state.set[index].energy) || 0;
      if (Math.abs(energy - target) > 1.5) {
        const x = state.set.length === 1 ? 0.5 : index / (state.set.length - 1);
        parts.push(`<circle cx="${xFor(x).toFixed(1)}" cy="${yFor(energy).toFixed(1)}" r="6" fill="none" stroke="var(--warn)" stroke-width="1.5"></circle>`);
      }
    });
  }

  points.forEach(([x, y], index) => {
    parts.push(`<circle class="handle" data-point="${index}" cx="${xFor(x).toFixed(1)}" cy="${yFor(y).toFixed(1)}" r="6"></circle>`);
  });

  svg.innerHTML = parts.join('');
  svg.querySelectorAll('.handle').forEach((handle) => {
    handle.addEventListener('pointerdown', (event) => startCurveDrag(event, Number(handle.getAttribute('data-point'))));
  });
}

function startCurveDrag(event, index) {
  event.preventDefault();
  const svg = $('#curveChart');
  const points = activeCurvePoints().map((p) => [p[0], p[1]]);
  const rect = svg.getBoundingClientRect();
  const scaleX = CHART.width / rect.width;
  const scaleY = CHART.height / rect.height;

  const onMove = (moveEvent) => {
    const x = (moveEvent.clientX - rect.left) * scaleX;
    const y = (moveEvent.clientY - rect.top) * scaleY;
    points[index] = [Number(positionFromX(x).toFixed(3)), Number(energyFromY(y).toFixed(2))];
    state.curvePoints = points.map((p) => [p[0], p[1]]);
    state.template = 'custom';
    $('#templateSel').value = 'custom';
    renderCurve();
  };
  const onUp = () => {
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    // Keep the curve sorted so interpolation stays sane.
    state.curvePoints.sort((a, b) => a[0] - b[0]);
    renderCurve();
  };
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
}

/* ------------------------------------------------------------- actions */

async function buildSet() {
  // Build from exactly what the pool shows, so the search box, "Local files
  // only" and the per-track checkboxes all mean what they look like.
  const keys = sortedPool().filter((track) => !state.excluded.has(track.key)).map((track) => track.key);
  if (!keys.length) {
    toast('No tracks available — check the pool filters, or clear the exclusions.', true);
    return;
  }
  try {
    const result = await api('/api/build', {
      playlist_id: state.playlistId,
      keys,
      config: currentConfig()
    });
    pushHistory();
    state.set = result.order || [];
    state.set.forEach((track) => state.trackIndex.set(track.key, track));
    state.lockedKeys = new Set();
    state.warnings = result.warnings || [];
    renderSet();
    renderPool();
    const stats = result.stats || {};
    toast(`Built ${stats.count || state.set.length} tracks · ${stats.harmonic_ok || 0}% harmonic · ${stats.beatmatched || 0}% within BPM tolerance`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function optimizeSet() {
  if (!state.set.length) { toast('Build a set first.', true); return; }
  const payload = {
    order: state.set.map((t) => t.key),
    config: { ...currentConfig(), template: state.template, curve_points: state.curvePoints },
    targets: targets()
  };
  try {
    const result = await api('/api/optimize', payload);
    pushHistory();
    state.set = result.order || state.set;
    state.set.forEach((track) => state.trackIndex.set(track.key, track));
    state.warnings = result.warnings || [];
    renderSet();
    renderPool();
    toast('Set optimized — pinned tracks stayed put.');
  } catch (error) {
    toast(error.message, true);
  }
}

/*
 * The native Mac app loads the page with ?shell=app. Its window is a WKWebView,
 * which ignores the blob-download trick below, so exports are written to disk by
 * the server instead and the path is reported in the toast.
 */
function isAppShell() {
  return new URLSearchParams(window.location.search).get('shell') === 'app';
}

/*
 * The pre-flight check: does every track still have a file behind it, and is
 * anything in the set twice. It never blocks an export -- the export is a copy,
 * and the person who built the set decides what belongs in it -- but the report
 * is shown before the file is written, and it travels inside the package.
 */
const PREFLIGHT_LABELS = {
  missing_file: 'no file',
  duplicate_track: 'listed twice',
  duplicate_file: 'same file',
  duplicate_name: 'same title',
  streaming: 'streaming',
  no_cues: 'no cues'
};

async function runPreflight({ open = false, cues } = {}) {
  if (!state.set.length) {
    if (open) toast('Build a set first — there is nothing to check.', true);
    return null;
  }
  const wantsCues = cues === undefined
    ? ($('#exportCues') ? $('#exportCues').checked : true)
    : cues;
  try {
    const report = await api('/api/preflight', {
      order: state.set.map((track) => track.key),
      name: ($('#setName').value || 'Parity Set').trim(),
      cues: wantsCues
    });
    state.preflight = report;
    const issues = report.issues || [];
    if (open || issues.length) renderPreflight(report);
    if (issues.length && !open) {
      const counts = report.counts || {};
      toast(
        `Pre-flight: ${counts.error || 0} error(s), ${counts.warning || 0} warning(s) — see the report`,
        Boolean(counts.error)
      );
    }
    return report;
  } catch (error) {
    if (open) toast(error.message, true);
    return null;
  }
}

function renderPreflight(report) {
  const issues = report.issues || [];
  const counts = report.counts || {};
  const status = $('#preflightStatus');
  status.textContent = issues.length
    ? `${counts.error || 0} error(s), ${counts.warning || 0} warning(s) in ${report.checked} tracks`
    : `All clear — ${report.checked} tracks checked`;
  status.classList.toggle('bad', Boolean(counts.error));

  const blocks = [];
  if (!issues.length) {
    blocks.push(
      '<div>Every track has a file behind it, nothing is in the set twice, ' +
      'and every track that needs cues has them.</div>'
    );
  } else {
    blocks.push(
      '<div>Nothing here blocks an export — this is what the export, the write and ' +
      `the package will carry. Set name: <b>${escapeHtml(($('#setName').value || 'Parity Set'))}</b>.</div>`
    );
    for (const severity of ['error', 'warning']) {
      const rows = issues.filter((issue) => issue.severity === severity);
      if (!rows.length) continue;
      blocks.push(`<h4>${severity === 'error' ? 'Errors' : 'Warnings'} — ${rows.length}</h4>`);
      blocks.push('<table>' + rows.map((issue) => (
        `<tr><td class="row-slot${severity === 'error' ? ' warn' : ''}">` +
        `${escapeHtml(PREFLIGHT_LABELS[issue.kind] || issue.kind)}</td>` +
        `<td>${escapeHtml(issue.label)}</td>` +
        `<td class="num">${escapeHtml((issue.positions || []).join(', '))}</td>` +
        `<td>${escapeHtml(issue.detail)}</td></tr>`
      )).join('') + '</table>');
    }
  }
  $('#preflightBody').innerHTML = blocks.join('');
  $('#preflightPanel').hidden = false;
}

async function exportSet(format) {
  if (format === 'preflight') {
    await runPreflight({ open: true });
    return;
  }
  if (!state.set.length) { toast('Nothing to export yet.', true); return; }
  const name = ($('#setName').value || 'Parity Set').trim();

  const cues = $('#exportCues') ? $('#exportCues').checked : true;
  const tags = $('#exportTags') ? $('#exportTags').checked : true;

  // Checked first, and never in the way: a missing file or a track listed twice
  // is worth seeing before the file lands, not after.
  await runPreflight({ cues });

  /*
   * The way into AUTO/PILOT is a rekordbox XML: a row written into its database
   * by hand lands in the library but never gets analysed. Saving the file is the
   * part the server has to do, since the app shell ignores blob downloads.
   */
  if (format === 'autopilot-xml') {
    try {
      const saved = await api('/api/export', {
        order: state.set.map((t) => t.key), name, format: 'rekordbox', cues, tags, save: true
      });
      toast(`Saved ${saved.filename} to ${saved.directory} — import it in AUTO/PILOT and it will analyse the tracks.`);
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }

  if (isAppShell()) {
    try {
      const saved = await api('/api/export', {
        order: state.set.map((t) => t.key), name, format, cues, tags, save: true
      });
      toast(`Saved ${saved.filename} to ${saved.directory}`);
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }

  try {
    const response = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order: state.set.map((t) => t.key), name, format, cues, tags })
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || 'Export failed');
    }
    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const match = /filename="([^"]+)"/.exec(disposition);
    const filename = match ? match[1] : `parity.${format}`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast(`Exported ${filename}`);
  } catch (error) {
    toast(error.message, true);
  }
}

/*
 * Duplicates: the same track twice in a set, one file behind two rows, or the
 * same artist and title on different files.  The check is on the set when there
 * is one (a repeated track costs a slot), otherwise on the pool.  The server
 * groups them; this panel only decides which row of each group stays.
 */
async function checkDuplicates() {
  const scope = state.set.length ? 'set' : 'pool';
  const payload = scope === 'set'
    ? { order: state.set.map((track) => track.key) }
    : { keys: state.pool.map((track) => track.key) };
  if (!payload.order && !payload.keys.length) {
    toast('Load a playlist first — there is nothing to check.', true);
    return null;
  }
  try {
    const report = await api('/api/duplicates', payload);
    state.duplicates = report;
    renderDuplicates(report);
    return report;
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}

function renderDuplicates(report) {
  const groups = report.groups || [];
  const status = $('#duplicatesStatus');
  status.textContent = groups.length
    ? `${report.group_count} duplicate group(s), ${report.duplicate_count} row(s) removable — in the ${report.scope}`
    : `No duplicates in the ${report.scope} (${report.checked} checked)`;
  status.classList.toggle('bad', groups.length > 0);

  if (!groups.length) {
    $('#duplicatesBody').innerHTML =
      '<div>Every track in this ' + escapeHtml(report.scope) + ' is unique. Nothing to remove.</div>';
    $('#duplicatesApply').hidden = true;
    $('#duplicatesPanel').hidden = false;
    return;
  }

  const blocks = groups.map((group) => {
    const rows = group.tracks.map((track) => (
      `<tr>
        <td class="num">${track.position}</td>
        <td>${track.kept ? '<span class="cue-badge">best</span>' : ''}</td>
        <td>${escapeHtml(track.label)}${track.streaming ? ' <span class="stream-badge">streaming</span>' : ''}</td>
        <td class="muted">${escapeHtml(track.path || '—')}</td>
        <td><label class="dup-pick"><input type="radio" name="dup-${escapeHtml(group.id)}"
             value="${track.position}" ${track.kept ? 'checked' : ''}> keep</label></td>
      </tr>`
    )).join('');
    return `<h4>${escapeHtml(group.reason)} — ${group.positions.length} rows</h4>
      <table><tr><th>#</th><th></th><th>Track</th><th>File</th><th>Keep</th></tr>${rows}</table>`;
  });
  blocks.unshift(
    '<div>Choose the row to keep in each group. Applying it removes the other rows from '
    + `<b>the ${escapeHtml(report.scope)}</b>; no file is deleted and the library is untouched.</div>`
  );
  $('#duplicatesBody').innerHTML = blocks.join('');
  $('#duplicatesApply').hidden = false;
  $('#duplicatesPanel').hidden = false;
}

async function applyDuplicates() {
  const report = state.duplicates;
  if (!report || !report.groups || !report.groups.length) return;
  const keep = {};
  report.groups.forEach((group) => {
    const chosen = document.querySelector(`input[name="dup-${group.id}"]:checked`);
    if (chosen) keep[group.id] = Number(chosen.value);
  });

  if (report.scope === 'pool') {
    const drop = new Set();
    report.groups.forEach((group) => {
      const chosen = keep[group.id] || group.keep_position;
      group.tracks.forEach((track) => {
        if (track.position !== chosen) drop.add(track.key);
      });
    });
    drop.forEach((key) => state.excluded.add(key));
    renderPool();
    $('#duplicatesPanel').hidden = true;
    toast(`${drop.size} duplicate row(s) unticked in the pool — the files are untouched.`);
    return;
  }

  try {
    const plan = await api('/api/duplicates/remove', {
      order: state.set.map((track) => track.key),
      keep
    });
    if (!plan.removed_count) {
      toast('Nothing was removed — the duplicates already agree with your choices.');
      return;
    }
    pushHistory();
    const held = new Map(state.set.map((track) => [track.key, track]));
    const next = plan.order
      .map((key) => held.get(key) || state.trackIndex.get(key))
      .filter(Boolean);
    state.set = next;
    renderSet();
    renderPool();
    $('#duplicatesPanel').hidden = true;
    toast(`Removed ${plan.removed_count} duplicate row(s) from the set.`);
  } catch (error) {
    toast(error.message, true);
  }
}

/*
 * The metadata editor.
 *
 * A track's title, artist and comment are what the library is searched by, so
 * they are editable here rather than only in rekordbox.  Saving without
 * "Write tags to the file" changes what Parity shows; with it, ffmpeg copies
 * the audio into a temp file with the new tags and replaces the original, so
 * the samples are untouched and the other apps can read the change.
 */
function openMetaEditor(track) {
  if (!track) return;
  state.metaKey = track.key;
  const fields = {
    title: track.title || '',
    artist: track.artist || '',
    album: track.album || '',
    genre: track.genre || '',
    comment: track.comments || '',
    year: track.year || '',
    track_number: track.track_number || '',
    bpm: track.bpm || '',
    key: track.camelot || track.tonality || ''
  };
  $('#metaTitle').value = fields.title;
  $('#metaArtist').value = fields.artist;
  $('#metaAlbum').value = fields.album;
  $('#metaGenre').value = fields.genre;
  $('#metaComment').value = fields.comment;
  $('#metaYear').value = fields.year;
  $('#metaTrack').value = fields.track_number;
  $('#metaBpm').value = fields.bpm;
  $('#metaKey').value = fields.key;
  $('#metaWriteTags').checked = false;
  // A streaming-only row has no file to tag, so the option is visibly off.
  const local = canPreview(track);
  $('#metaWriteTags').disabled = !local;
  $('#metaStatus').textContent = track.streaming
    ? 'streaming only — the library record can change, the file cannot'
    : track.path || '';
  $('#metaPanel').hidden = false;
  $('#metaTitle').focus();
}

async function saveMetaEditor() {
  const key = state.metaKey;
  if (!key) return;
  const fields = {
    title: $('#metaTitle').value,
    artist: $('#metaArtist').value,
    album: $('#metaAlbum').value,
    genre: $('#metaGenre').value,
    comment: $('#metaComment').value,
    year: $('#metaYear').value,
    track_number: $('#metaTrack').value,
    bpm: $('#metaBpm').value,
    key: $('#metaKey').value
  };
  const writeTags = $('#metaWriteTags').checked;
  try {
    const result = await api('/api/track/edit', { key, fields, write_tags: writeTags });
    const tags = result.tags || {};
    if (writeTags && tags.missing && tags.missing.length) {
      toast(`Saved, but this container cannot hold: ${tags.missing.join(', ')}`, true);
    } else {
      toast(writeTags ? 'Metadata saved and written to the file.' : 'Metadata saved.');
    }
    $('#metaPanel').hidden = true;
    state.metaKey = null;
    await refreshTracks();
  } catch (error) {
    toast(error.message, true);
  }
}

async function saveSet() {
  if (!state.set.length) { toast('Nothing to save yet.', true); return; }
  try {
    const saved = await api('/api/sets', {
      name: ($('#setName').value || 'Parity Set').trim(),
      playlist_id: state.playlistId,
      order: state.set.map((t) => t.key),
      locks: buildLocks(),
      config: currentConfig()
    });
    // Saving makes this the loaded set, so Delete refers to the set you just
    // saved instead of asking for a trip through the dropdown first.
    if (saved.set && saved.set.id) state.loadedSetId = saved.set.id;
    state.meta = await api('/api/state');
    renderSavedSets();
    toast('Set saved locally.');
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadSet(setId) {
  try {
    const data = await api('/api/sets');
    const record = (data.sets || []).find((item) => item.id === setId);
    if (!record) return;
    if (!state.trackIndex.size) await selectPlaylist(null, 'All tracks', true);
    if (!state.trackIndex.has(record.order[0])) {
      const all = await api('/api/tracks');
      (all.tracks || []).forEach((track) => state.trackIndex.set(track.key, track));
    }
    pushHistory();
    state.set = (record.order || []).map((key) => state.trackIndex.get(key)).filter(Boolean);
    state.lockedKeys = new Set(Object.values(record.locks || {}));
    if (record.config && record.config.curve_points) {
      state.curvePoints = record.config.curve_points.map((p) => [p[0], p[1]]);
      state.template = 'custom';
      $('#templateSel').value = 'custom';
    }
    $('#setName').value = record.name;
    state.loadedSetId = record.id;
    state.warnings = [];
    renderSet();
    renderPool();
    toast(`Loaded “${record.name}” (${state.set.length} tracks)`);
  } catch (error) {
    toast(error.message, true);
  }
}

/* -------------------------------------------- rekordbox pipeline actions */

/*
 * Deleting a saved set is local and small, so it uses the two-click arm rather
 * than the report panel the database edits go through: the first click arms the
 * button, the second one within a few seconds deletes.
 */
function armButton(button, label, action) {
  if (button.dataset.armed === '1') {
    button.dataset.armed = '';
    clearTimeout(button._armTimer);
    button.textContent = button.dataset.label || label;
    action();
    return;
  }
  button.dataset.label = button.dataset.label || button.textContent;
  button.dataset.armed = '1';
  button.textContent = label;
  button._armTimer = setTimeout(() => {
    button.dataset.armed = '';
    button.textContent = button.dataset.label;
  }, 6000);
}

async function deleteSet() {
  const id = state.loadedSetId;
  const name = $('#setName').value || 'this set';
  if (!id) { toast('Load a saved set first — the dropdown on the left.', true); return; }
  try {
    await api('/api/sets/delete', { id });
    state.loadedSetId = null;
    state.meta = await api('/api/state');
    renderSavedSets();
    toast(`Deleted the saved set “${name}”.`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function refreshTracks() {
  state.meta = await api('/api/state');
  renderCueStatus();
  renderAudioStatus();
  renderAnalysisStatus();
  renderLibraryStatus();
  const query = state.playlistId ? `?playlist=${encodeURIComponent(state.playlistId)}` : '';
  const data = await api(`/api/tracks${query}`);
  state.pool = data.tracks || [];
  state.trackIndex = new Map(state.pool.map((track) => [track.key, track]));
  state.set = state.set.map((track) => state.trackIndex.get(track.key) || track);
  renderPool(true);
  renderSet();
  // A cue run may have just given the previewed track its jump targets.
  if (player.track) loadCueMarkers(player.track);
}

/*
 * The measured-energy status line: how many local tracks have had their audio
 * read, and the ffmpeg hint when this machine cannot read any of them.
 */
function renderAudioStatus() {
  const node = $('#audioStatus');
  if (!node) return;
  const audio = (state.meta || {}).audio || {};
  if (!state.meta || !state.meta.has_library) {
    node.textContent = '';
    return;
  }
  if (!audio.available) {
    // Two requirements, two failures: name the one that is actually missing
    // rather than sending the user to install ffmpeg they already have.
    node.textContent = audio.ffmpeg_ok === false
      ? 'Energy from audio needs ffmpeg'
      : 'Energy from audio needs numpy';
    node.title = (audio.ffmpeg_ok === false ? audio.ffmpeg_error : audio.numpy_error)
      || audio.error || '';
    return;
  }
  const local = audio.local || 0;
  const measured = audio.analysed || 0;
  const engine = audio.engine === 'librosa' ? 'librosa' : 'ffmpeg';
  node.textContent = measured
    ? `${measured} of ${local} local tracks measured (${engine})`
    : 'Energy from audio not measured yet';
  node.title = measured
    ? 'These tracks use their measured energy. Type a value in the Energy column to override one.'
    : `Read loudness, brightness, dynamic range and drive from each local file with ${engine}.`
    + (audio.stale ? ` ${audio.stale} stored measurement(s) are from an older model and will be redone.` : '');
}

/*
 * The tempo/key status line: how many local tracks have a measured BPM, key
 * and grid, and the missing half of the stack when this machine cannot run it.
 */
function renderAnalysisStatus() {
  const node = $('#analysisStatus');
  if (!node) return;
  const info = (state.meta || {}).analysis || {};
  if (!state.meta || !state.meta.has_library) {
    node.textContent = '';
    return;
  }
  if (!info.available) {
    node.textContent = info.ffmpeg_ok === false
      ? 'BPM/key analysis needs ffmpeg'
      : 'BPM/key analysis needs numpy';
    node.title = (info.ffmpeg_ok === false ? info.ffmpeg_error : info.numpy_error)
      || info.error || '';
    return;
  }
  const measured = info.analysed || 0;
  const local = info.local || 0;
  node.textContent = measured
    ? `${measured} of ${local} local tracks analysed`
    : 'BPM/key not analysed yet';
  node.title = measured
    ? `${info.using_bpm || 0} track(s) take their BPM from the audio and ${info.using_key || 0} their key; the library's own values always win.`
    : 'Read BPM, key and a beat grid from each local file with ffmpeg and numpy.';
}

async function analyseTrackAudio() {
  const button = $('#analyseAudioBtn');
  const status = $('#analysisStatus');
  const inPlay = (state.set.length ? state.set : state.pool)
    .filter((track) => !track.streaming);
  if (!inPlay.length) {
    toast('Every track in play is streaming only — there is no file to analyse.', true);
    return;
  }
  let remaining = inPlay.map((track) => track.key);
  const total = inPlay.length;
  button.disabled = true;
  const previous = status.textContent;
  const bpms = [];
  const keys = [];
  const reasons = [];
  let measured = 0;
  let cached = 0;
  let skipped = 0;
  try {
    for (let round = 0; round < 200 && remaining.length; round += 1) {
      const result = await api('/api/analyze', { keys: remaining, local_only: true, limit: 8 });
      const rows = result.results || [];
      const unreadable = result.skipped || [];
      const settled = new Set([
        ...rows.map((row) => row.key),
        ...(result.cached_keys || []),
        ...unreadable.map((row) => row.key)
      ]);
      measured += result.analysed;
      cached += (result.cached_keys || []).length;
      skipped += unreadable.length;
      rows.forEach((row) => {
        if (row.bpm) bpms.push(row.bpm);
        if (row.camelot) keys.push(row.camelot);
        if (row.note) reasons.push(row.note);
      });
      unreadable.forEach((row) => { if (row.reason) reasons.push(row.reason); });
      remaining = remaining.filter((key) => !settled.has(key));
      status.textContent = `Analysing ${measured} of ${total} local tracks…`;
      if (!result.analysed || !settled.size) break;
    }
    if (measured) {
      const spread = bpms.length
        ? `${bpms.length} BPM values ${Math.min(...bpms).toFixed(1)}–${Math.max(...bpms).toFixed(1)}`
        : 'no tempo found';
      toast(`Analysed ${measured} of ${total} tracks${cached ? `, ${cached} already cached` : ''} — ${spread}`
        + (keys.length ? `, ${new Set(keys).size} key(s)` : ''));
    } else if (skipped) {
      status.textContent = `${skipped} of ${total} tracks could not be analysed`;
      toast(`${skipped} of ${total} tracks could not be analysed — ${reasons[0] || 'unknown reason'}`, true);
    } else {
      status.textContent = previous;
      toast(`All ${total} tracks in play are already analysed — nothing to redo.`);
    }
    await refreshTracks();
  } catch (error) {
    status.textContent = previous;
    toast(error.message, true);
  } finally {
    button.disabled = false;
    renderAnalysisStatus();
  }
}

function audioSpread(energies) {
  if (!energies.length) return '';
  const sorted = [...energies].sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)];
  return `E${sorted[0].toFixed(1)} to E${sorted[sorted.length - 1].toFixed(1)}, median E${median.toFixed(1)}`;
}

/*
 * Measure the audio for the tracks in play, in chunks, so a 500-track library
 * shows progress instead of freezing the page for two minutes.  The server
 * caches by file, so a second press only redoes what changed.
 */
async function measureEnergy() {
  const button = $('#audioEnergyBtn');
  const status = $('#audioStatus');
  // Only the local tracks in play.  Sending the whole pool and letting the
  // server drop the streaming ones meant "nothing to do" and "all done" looked
  // identical from here -- and the status line reported the library's local
  // count, so it said "0 of 116 measured" for a pool of forty streaming entries.
  const inPlay = (state.set.length ? state.set : state.pool)
    .filter((track) => !track.streaming);
  if (!inPlay.length) {
    status.textContent = 'No local files in play to measure';
    toast('Every track in play is streaming only — there is no file to measure. '
      + 'Turn off “Local files only” or load a collection with real file paths.', true);
    return;
  }
  // Only what is still unsettled is sent.  A round settles some keys and the
  // server names them back -- measured, already cached, or unreadable -- so each
  // track is counted once and the next round does not redo (or re-count) it.
  // Sending the whole pool every round is what made a 14-track pool report
  // "26 already cached": the same dozen cached keys were counted again in each
  // later chunk.
  let remaining = inPlay.map((track) => track.key);
  const total = inPlay.length;
  button.disabled = true;
  const previous = status.textContent;
  const energies = [];
  const reasons = [];
  let measured = 0;
  let cached = 0;
  let skipped = 0;
  try {
    for (let round = 0; round < 200 && remaining.length; round += 1) {
      const result = await api('/api/energy/audio', {
        keys: remaining, local_only: true, limit: 12
      });
      const results = result.results || [];
      const unreadable = result.skipped || [];
      const settled = new Set([
        ...results.map((row) => row.key),
        ...(result.cached_keys || []),
        ...unreadable.map((row) => row.key)
      ]);
      measured += result.analysed;
      cached += (result.cached_keys || []).length;
      skipped += unreadable.length;
      unreadable.forEach((row) => { if (row.reason) reasons.push(row.reason); });
      results.forEach((row) => energies.push(row.energy));
      remaining = remaining.filter((key) => !settled.has(key));
      status.textContent = `Measuring ${measured} of ${total} local tracks…`;
      // Nothing analysed and nothing settled means the rest of the pool is not
      // the server's to answer for (unknown keys), so asking again would spin.
      if (!result.analysed || !settled.size) break;
    }
    if (measured) {
      toast(`Measured ${measured} of ${total} tracks${cached ? `, ${cached} already cached` : ''}`
        + ` — ${audioSpread(energies)}`);
    } else if (skipped) {
      // Files the decoder refused: a missing file or a format it cannot read.
      status.textContent = `${skipped} of ${total} tracks could not be read`;
      toast(`${skipped} of ${total} tracks could not be read — ${reasons[0] || 'unknown reason'}`, true);
    } else {
      status.textContent = previous;
      toast(`All ${total} tracks in play are already measured — nothing to redo.`, false);
    }
    await refreshTracks();
  } catch (error) {
    status.textContent = previous;
    toast(error.message, true);
  } finally {
    button.disabled = false;
    renderAudioStatus();
  }
}

async function analyseCues() {
  const button = $('#analyseBtn');
  button.disabled = true;
  try {
    // Analyse what the set already uses, otherwise the filtered pool.
    const payload = state.set.length
      ? { keys: state.set.map((track) => track.key) }
      : { filter: currentFilter() };
    const result = await api('/api/cues', payload);
    const sources = Object.entries(result.by_source || {})
      .map(([name, count]) => `${count} ${name === 'rekordbox-anlz' ? 'from rekordbox analysis' : 'estimated'}`)
      .join(', ');
    toast(`Cues for ${result.analysed} tracks in ${result.elapsed}s${sources ? ` — ${sources}` : ''}`);
    await refreshTracks();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

/* --------------------------------------------------------------- events */

function attachEvents() {
  $('#analyseBtn').addEventListener('click', analyseCues);
  const duplicateButton = $('#duplicatesBtn');
  if (duplicateButton) duplicateButton.addEventListener('click', checkDuplicates);
  const applyDuplicatesButton = $('#duplicatesApply');
  if (applyDuplicatesButton) applyDuplicatesButton.addEventListener('click', applyDuplicates);
  const duplicatesClose = $('#duplicatesClose');
  if (duplicatesClose) duplicatesClose.addEventListener('click', () => { $('#duplicatesPanel').hidden = true; });
  const audioButton = $('#audioEnergyBtn');
  if (audioButton) audioButton.addEventListener('click', measureEnergy);
  const analyseButton = $('#analyseAudioBtn');
  if (analyseButton) analyseButton.addEventListener('click', analyseTrackAudio);
  const policySelect = $('#writeCuePolicy');
  if (policySelect) {
    policySelect.addEventListener('change', async (event) => {
      const value = event.target.value;
      if (await setCuePolicy({ policy: value })) {
        toast(value === 'keep'
          ? 'Tracks already cued by hand will be left exactly as they are.'
          : 'Every track in the set gets hot cues A–H on the next write.');
      } else {
        event.target.value = state.cuePolicy;
      }
    });
  }
  $('#folderSel').addEventListener('change', (event) => {
    state.folder = event.target.value;
    renderPool(true);
  });
  $('#cuesOnly').addEventListener('change', (event) => {
    state.cuesOnly = event.target.checked;
    renderPool(true);
  });
  /*
   * The XML import, for the case the database cannot answer.  A Finder drag
   * carries "Files" in its types and the set rows' own drag carries text/plain,
   * so this stays out of the way of reordering the set by dragging a row.
   */
  const draggedFiles = (event) => Array.from(
    (event.dataTransfer && event.dataTransfer.types) || []
  ).includes('Files');
  window.addEventListener('dragover', (event) => {
    if (draggedFiles(event)) event.preventDefault();
  });
  window.addEventListener('drop', async (event) => {
    if (!draggedFiles(event)) return;
    event.preventDefault();
    const file = event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) await importFile(file);
  });

  $('#reloadBtn').addEventListener('click', async () => {
    try {
      state.meta = await api('/api/reload', {});
      await loadState();
      toast(state.meta.source_kind === 'db' ? 'Re-read master.db.' : 'Re-read the stored XML.');
    } catch (error) {
      toast(error.message, true);
    }
  });

  // A 5000 track playlist should not re-sort and re-render on every keystroke.
  let searchTimer = null;
  $('#poolSearch').addEventListener('input', (event) => {
    const value = event.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.search = value; renderPool(true); }, 130);
  });
  $('#poolSort').addEventListener('change', (event) => { state.sort = event.target.value; renderPool(true); });
  $('#localOnly').addEventListener('change', (event) => {
    state.localOnly = event.target.checked;
    renderPool(true);
  });

  const poolList = $('#trackList');
  poolList.addEventListener('scroll', () => {
    if (poolScrollFrame) return;
    poolScrollFrame = requestAnimationFrame(() => {
      poolScrollFrame = null;
      renderPoolWindow();
    });
  }, { passive: true });
  poolList.addEventListener('click', (event) => {
    if (previewButton(event, '.track-row')) return;
    const row = event.target.closest('.track-row');
    const track = row && state.trackIndex.get(row.getAttribute('data-key'));
    // The cue editor belongs to a track, not to the set: every row in the pool
    // can be opened, whether or not it has been built into a set yet.
    if (event.target.closest('[data-act="cues"]')) {
      if (track) openCueEditor(track);
      return;
    }
    if (event.target.closest('[data-act="edit"]')) {
      if (track) openMetaEditor(track);
      return;
    }
    if (event.target.closest('[data-act="queue"]')) {
      if (track) queueTrack(track);
      return;
    }
    if (!event.target.closest('[data-act="add"]')) return;
    if (!track) return;
    pushHistory();
    state.set.push(track);
    renderSet();
    renderPool();
  });
  poolList.addEventListener('change', async (event) => {
    const act = event.target.getAttribute('data-act');
    const row = event.target.closest('.track-row');
    const track = row && state.trackIndex.get(row.getAttribute('data-key'));
    if (!act || !track) return;
    if (act === 'include') {
      if (event.target.checked) state.excluded.delete(track.key);
      else state.excluded.add(track.key);
      row.classList.toggle('dim', !event.target.checked);
    } else if (act === 'energy') {
      const value = Number(event.target.value);
      if (value) await setEnergy(track, value);
    }
  });

  $('#templateSel').addEventListener('change', (event) => {
    state.template = event.target.value;
    state.curvePoints = state.template === 'custom' ? state.curvePoints || templatePoints() : null;
    renderCurve();
  });
  $('#resetCurveBtn').addEventListener('click', () => {
    state.curvePoints = null;
    renderCurve();
  });

  $('#lengthRange').addEventListener('input', (event) => setSetLength(Number(event.target.value)));
  $('#lengthInput').addEventListener('change', (event) => setSetLength(Number(event.target.value) || 20));

  const sliders = [
    ['#energyWeight', '#energyOut', (v) => Number(v).toFixed(1)],
    ['#keyWeight', '#keyOut', (v) => Number(v).toFixed(1)],
    ['#bpmWeight', '#bpmOut', (v) => Number(v).toFixed(1)],
    ['#bpmTolerance', '#tolOut', (v) => v],
    ['#artistWindow', '#artistOut', (v) => v]
  ];
  sliders.forEach(([input, output, format]) => {
    $(input).addEventListener('input', (event) => {
      $(output).textContent = format(event.target.value);
      if (input === '#bpmTolerance') renderSet();
    });
  });

  $('#buildBtn').addEventListener('click', buildSet);
  $('#optimizeBtn').addEventListener('click', optimizeSet);
  $('#clearBtn').addEventListener('click', () => {
    if (!state.set.length) return;
    pushHistory();
    state.set = [];
    state.lockedKeys = new Set();
    state.warnings = [];
    renderSet();
    renderPool();
  });
  $('#undoBtn').addEventListener('click', undo);
  $('#saveSetBtn').addEventListener('click', saveSet);
  $('#deleteSetBtn').addEventListener('click', (event) => {
    const button = event.currentTarget;
    if (!state.loadedSetId) {
      toast('Load a saved set first — the dropdown on the left.', true);
      return;
    }
    armButton(button, 'Click again to delete', deleteSet);
  });
  $('#savedSets').addEventListener('change', (event) => {
    if (event.target.value) loadSet(event.target.value);
    event.target.value = '';
  });

  // One button, two panels: they are alternatives, and a toolbar button each
  // made them look like two unrelated features.
  $('#toolsBtn').addEventListener('click', (event) => {
    event.stopPropagation();
    $('#toolsMenu').hidden = !$('#toolsMenu').hidden;
  });
  $('#toolsMenu').addEventListener('click', (event) => event.stopPropagation());
  document.addEventListener('click', () => { $('#toolsMenu').hidden = true; });
  $('#toolsMenu').querySelectorAll('button[data-tool]').forEach((button) => {
    button.addEventListener('click', () => {
      $('#toolsMenu').hidden = true;
      const tool = button.getAttribute('data-tool');
      if (tool === 'manage') toggleManage();
      else if (tool === 'ingest') toggleIngest();
      else if (tool === 'watch') toggleWatch();
    });
  });
  $('#manageClose').addEventListener('click', () => { $('#managePanel').hidden = true; });
  $('#ingestClose').addEventListener('click', () => { $('#ingestPanel').hidden = true; });
  $('#watchClose').addEventListener('click', () => { $('#watchPanel').hidden = true; });
  $('#watchSave').addEventListener('click', saveWatch);
  $('#watchScan').addEventListener('click', scanWatch);
  $('#watchPathPick').addEventListener('click', () => pickFolder('watch'));
  $('#m3uBtn').addEventListener('click', () => $('#m3uPick').click());
  $('#m3uPick').addEventListener('change', async (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = '';
    if (file) await importM3UFile(file);
  });
  $('#ingestSourcePick').addEventListener('click', () => pickFolder('source'));
  $('#ingestDestinationPick').addEventListener('click', () => pickFolder('destination'));
  $('#ingestScanBtn').addEventListener('click', () => ingestScan());
  $('#ingestRunBtn').addEventListener('click', () => ingestRun());
  ['#ingestFormat', '#ingestRecursive', '#ingestMeasure', '#ingestTrim', '#ingestOverwrite']
    .forEach((sel) => $(sel).addEventListener('change', () => {
      // Any switch invalidates the scan it was made against.
      $('#ingestRunBtn').hidden = true;
      $('#ingestSummary').textContent = '';
    }));
  ['#ingestSource', '#ingestDestination'].forEach((sel) => $(sel).addEventListener('input', () => {
    $('#ingestRunBtn').hidden = true;
    $('#ingestSummary').textContent = '';
  }));
  $('#rxPlaylists').addEventListener('click', (event) => manageClick(event, 'rekordbox'));
  $('#apPlaylists').addEventListener('click', (event) => manageClick(event, 'autopilot'));
  $('#rxDeleteTracks').addEventListener('click', () => deleteSetTracks('rekordbox'));
  $('#apDeleteTracks').addEventListener('click', () => deleteSetTracks('autopilot'));
  $('#metaClose').addEventListener('click', () => { $('#metaPanel').hidden = true; });
  $('#metaSave').addEventListener('click', saveMetaEditor);
  $('#reportCommit').addEventListener('click', commitPending);
  $('#reportCancel').addEventListener('click', clearPending);

  $('#exportBtn').addEventListener('click', (event) => {
    event.stopPropagation();
    $('#exportMenu').hidden = !$('#exportMenu').hidden;
  });
  document.addEventListener('click', () => { $('#exportMenu').hidden = true; });
  $('#exportMenu').querySelectorAll('button').forEach((button) => {
    button.addEventListener('click', () => {
      $('#exportMenu').hidden = true;
      exportSet(button.getAttribute('data-format'));
    });
  });

  $('#writeBtn').addEventListener('click', async (event) => {
    event.stopPropagation();
    const menu = $('#writeMenu');
    menu.hidden = !menu.hidden;
    if (!menu.hidden) {
      $('#writeHint').textContent = 'Checking the libraries…';
      await refreshWriteStatus();
    }
  });
  $('#writeMenu').addEventListener('click', (event) => event.stopPropagation());
  document.addEventListener('click', () => { $('#writeMenu').hidden = true; });
  $('#writeMenu').querySelectorAll('button[data-write]').forEach((button) => {
    button.addEventListener('click', () => {
      const target = button.getAttribute('data-write');
      $('#writeMenu').hidden = true;
      runWrite(target, target === 'dry');
    });
  });
  $('#writeReportClose').addEventListener('click', () => { $('#writeReport').hidden = true; });
  $('#preflightClose').addEventListener('click', () => { $('#preflightPanel').hidden = true; });

  document.addEventListener('keydown', (event) => {
    const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes((event.target.tagName || '').toUpperCase());
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
      event.preventDefault();
      undo();
    }
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      buildSet();
    }
    // Space toggles the preview; the transport keys only when one is open.
    if (!typing && event.code === 'Space') {
      event.preventDefault();
      previewPlayPause();
    }
    if (!typing && player.track) {
      if (event.key === '[') previewPreviousCue();
      if (event.key === ']') previewNextCue();
      if (event.key.toLowerCase() === 'l') { event.preventDefault(); toggleLoop(); }
      if (event.key === ',') { event.preventDefault(); jumpBeats(-1); }
      if (event.key === '.') { event.preventDefault(); jumpBeats(1); }
    }
    if (event.key === 'Escape' && player.track) stopPreview();
  });
}

/* --------------------------------------------------- direct writes (db) */

/*
 * Writing into master.db or AUTO/PILOT's library replaces rows those apps own,
 * so it is never a side effect of something else: the write menu opens, both
 * libraries are checked, a dry run lists every row, and only then does an
 * explicit click change anything. The report panel is that dry run made
 * readable -- the same rows the server logs.
 */

async function refreshWriteStatus() {
  try {
    state.writeStatus = await api('/api/write/status');
  } catch {
    state.writeStatus = null;
  }
  renderWriteHint();
}

function renderWriteHint() {
  const node = $('#writeHint');
  const status = state.writeStatus;
  if (!status) {
    node.textContent = 'Could not read the library status.';
    node.classList.add('bad');
    return;
  }
  const rx = status.rekordbox || {};
  const ap = status.autopilot || {};
  const rxPid = (rx.running || [])[0];
  const apPid = (ap.running || [])[0];
  const lines = [
    `rekordbox <b>${rxPid ? `open (pid ${rxPid.pid})` : 'closed'}</b> · ` +
      `${rx.exists ? `${(rx.size / 1048576).toFixed(1)} MB` : 'no master.db'}`,
    `AUTO/PILOT <b>${apPid ? `open (pid ${apPid.pid})` : 'closed'}</b> · ` +
      `${ap.exists ? `${ap.tracks} tracks` : 'no library'}`
  ];
  if (rxPid || apPid) lines.push('<span class="bad">Quit the app to write to its database.</span>');
  node.innerHTML = lines.join('<br>');
  node.classList.toggle('bad', Boolean(rxPid || apPid));
}

function plannedWritePayload(dryRun) {
  const name = $('#setName').value || 'Parity Set';
  return {
    order: state.set.map((track) => track.key),
    name,
    playlist: ($('#writePlaylist').checked || dryRun) ? name : '',
    cues: true,
    memory: $('#writeMemory').checked,
    // Off unless asked for: the comment is the user's own field, and this is the
    // one thing the write changes that they will read in rekordbox later.
    tags: $('#writeTags') ? $('#writeTags').checked : false,
    dry_run: Boolean(dryRun)
  };
}

async function runWrite(target, dryRun) {
  if (!state.set.length) {
    toast('Build a set first — there is nothing to write.', true);
    return;
  }
  const payload = plannedWritePayload(dryRun);
  // The dry run previews the rekordbox write: it is the one that carries the
  // cues, and its report is the row-by-row list worth reading before committing.
  const primary = target === 'dry' ? 'rekordbox' : target;
  const jobs = primary === 'both' ? ['rekordbox', 'autopilot'] : [primary];
  const results = [];
  for (const job of jobs) {
    try {
      results.push({ job, report: await api(`/api/write/${job}`, payload) });
    } catch (error) {
      results.push({ job, error: error.message || 'Write failed.' });
    }
  }
  renderWriteReport(results, dryRun);
  const failures = results.filter((item) => item.error);
  if (failures.length) {
    toast(`${failures[0].job}: ${failures[0].error}`.slice(0, 200), true);
  } else if (dryRun) {
    const report = results[0].report;
    const cues = report.cues ? `${report.cues.created} cue rows, ` : '';
    const kept = report.cues && (report.cues.kept || []).length
      ? `${report.cues.kept.length} tracks left with their own cues, `
      : '';
    const rows = report.playlist ? `${report.playlist.tracks} playlist rows` : 'no playlist change';
    toast(`Dry run — ${cues}${kept}${rows}, nothing written.`);
  } else {
    toast(results.map((item) => `${item.job}: ${writeSummary(item.report)}`).join(' · ').slice(0, 240));
  }
  refreshWriteStatus();
}

function writeSummary(report) {
  const parts = [];    if (report.cues) {
      parts.push(`${report.cues.created} cues`);
      if ((report.cues.kept || []).length) parts.push(`${report.cues.kept.length} kept as they are`);
    }
    if (report.tags && report.tags.changed) parts.push(`${report.tags.changed} tags`);
    if (report.tracks) parts.push(`${report.tracks.inserted + report.tracks.updated} tracks`);
  if (report.playlist) parts.push(`playlist ${report.playlist.name}`);
  if (report.backup) parts.push(`backup ${report.backup.split('/').pop()}`);
  return parts.join(', ') || 'nothing to do';
}

function renderWriteReport(results, dryRun) {
  clearPending();
  const esc = (value) => String(value === undefined || value === null ? '' : value)
    .replace(/[&<>]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[char]));
  const blocks = results.map(({ job, report, error }) => {
    if (error) return `<h4>${esc(job)}</h4><div class="warn">${esc(error)}</div>`;
    const out = [`<h4>${esc(job)} · ${report.dry_run ? 'dry run' : 'written'}</h4>`];
    out.push(`<div>${esc(report.database)}</div>`);
    if (report.backup) out.push(`<div>backup: ${esc(report.backup)}</div>`);
    if (report.playlist) {
      const pl = report.playlist;
      const written = pl.written === undefined ? pl.added : pl.written;
      out.push(
        `<div>playlist ${esc(pl.name)} <span class="num">id ${esc(pl.id)}</span> — ` +
        `${pl.tracks} tracks, ${pl.removed} removed, ${written} written` +
        `${pl.created ? ' (created)' : ''}${pl.rewritten ? ' (reordered)' : ' (already in order)'}</div>`
      );
    }
    if (report.tracks) {
      out.push(`<div>${report.tracks.inserted} tracks inserted, ${report.tracks.updated} refreshed</div>`);
    }
    if (report.cues) {
      const cues = report.cues;
      const kept = cues.kept || [];
      out.push(
        `<div>${cues.created} cue rows, ${cues.replaced} replaced, ` +
        `${cues.tracks} tracks cued, ${(cues.skipped || []).length} skipped` +
        `${kept.length ? `, ${kept.length} left with their own cues` : ''}</div>`
      );
      if (kept.length) {
        out.push('<table>' + kept.slice(0, 12).map((row) => (
          `<tr><td class="row-slot">keep</td>` +
          `<td>${esc(row.label)}</td>` +
          `<td class="num">${row.cues} cue${row.cues === 1 ? '' : 's'}</td>` +
          `<td class="num">${(row.seconds || []).slice(0, 3).map((s) => `${Number(s).toFixed(1)}s`).join(' ')}</td></tr>`
        )).join('') + '</table>');
        if (kept.length > 12) {
          out.push(`<div class="num">… ${kept.length - 12} more kept tracks</div>`);
        }
      }
      if (cues.rows.length) {
        const shown = cues.rows.slice(0, 40);
        out.push('<table>' + shown.map((row) => (
          `<tr><td class="row-slot">${esc(row.slot)}</td>` +
          `<td class="num">${Number(row.seconds).toFixed(3)}s</td>` +
          `<td>${esc(row.comment)}</td>` +
          `<td class="num">c${esc(row.colour_index)}</td>` +
          `<td>${esc(row.label)}</td></tr>`
        )).join('') + '</table>');
        if (cues.rows.length > shown.length) {
          out.push(`<div class="num">… ${cues.rows.length - shown.length} more cue rows</div>`);
        }
      }
      out.push(skippedTable(cues.skipped, esc));
    }
    if (report.sidecars) {
      out.push(
        `<div>sidecars: ${report.sidecars.present} present, ` +
        `${report.sidecars.missing} missing (AUTO/PILOT writes those)</div>`
      );
    }
    if (report.tags && (report.tags.changed || (report.tags.rows || []).length)) {
      const tags = report.tags;
      const roles = {};
      for (const row of ((report.tag_plan || {}).rows || [])) roles[row.key] = row.role;
      out.push(
        `<h4>comments</h4><div>${tags.changed} tag(s) written, ` +
        `${tags.unchanged} already tagged (nothing to change)</div>`
      );
      out.push('<table>' + tags.rows.slice(0, 30).map((row) => (
        `<tr><td class="row-slot">${esc(row.tag)}</td>` +
        `<td>${esc(row.label)}</td>` +
        `<td class="num">${esc(roles[row.key] || '')}</td>` +
        `<td>${esc(row.after)}</td></tr>`
      )).join('') + '</table>');
      if (tags.rows.length > 30) out.push(`<div class="num">… ${tags.rows.length - 30} more comment rows</div>`);
      if ((tags.over_limit || []).length) {
        out.push(`<div class="warn">${tags.over_limit.length} comment(s) are longer than 255 characters.</div>`);
      }
    }
    if (report.preflight) {
      const check = report.preflight;
      const counts = check.counts || {};
      const issues = (check.issues || []).slice(0, 8);
      out.push('<h4>pre-flight</h4>');
      out.push(issues.length
        ? `<div>${counts.error || 0} error(s), ${counts.warning || 0} warning(s) in ${check.checked} tracks — nothing here blocks the write.</div>`
        : `<div>${check.checked} tracks checked: every file present, no duplicates.</div>`);
      if (issues.length) {
        out.push('<table>' + issues.map((issue) => (
          `<tr><td class="row-slot${issue.severity === 'error' ? ' warn' : ''}">` +
          `${esc(PREFLIGHT_LABELS[issue.kind] || issue.kind)}</td>` +
          `<td>${esc(issue.label)}</td>` +
          `<td>${esc(issue.detail)}</td></tr>`
        )).join('') + '</table>');
        if ((check.issues || []).length > issues.length) {
          out.push(`<div class="num">… ${check.issues.length - issues.length} more pre-flight rows</div>`);
        }
      }
    }
    if (report.tracks) out.push(skippedTable(report.skipped, esc));
    for (const warning of report.warnings || []) {
      out.push(`<div class="warn">note: ${esc(warning)}</div>`);
    }
    return out.join('');
  });

  const committed = results.some((item) => item.report && !item.report.dry_run);
  $('#writeReportTitle').textContent = committed ? 'Write report — committed' : 'Write report — dry run';
  $('#writeReportBody').innerHTML = blocks.join('') +
    (dryRun ? '<div class="num">nothing was written.</div>' : '');
  $('#writeReport').hidden = false;
}

function skippedTable(rows, esc) {
  if (!(rows || []).length) return '';
  return '<table>' + rows.slice(0, 12).map((row) => (
    `<tr><td class="warn">skipped</td><td>${esc(row.label)}</td>` +
    `<td class="num">${esc(row.reason)}</td></tr>`
  )).join('') + '</table>';
}

/* --------------------------------------------------- library management */

/*
 * Renaming a playlist, deleting one, and deleting tracks all take something out
 * of a library Parity does not own, so none of them happens on one click: the
 * action runs as a dry run, the report lists the exact rows that would go, and
 * "Write it" re-runs it for real.  Audio files are never touched.
 */

async function toggleManage() {
  const panel = $('#managePanel');
  panel.hidden = !panel.hidden;
  if (!panel.hidden) {
    $('#ingestPanel').hidden = true;
    closeCueEditor();
    await refreshManage();
  }
}

async function refreshManage() {
  $('#manageStatus').textContent = 'Reading both libraries…';
  try {
    state.manage = await api('/api/library');
  } catch (error) {
    state.manage = null;
    $('#manageStatus').textContent = error.message;
    $('#manageStatus').classList.add('bad');
    return;
  }
  $('#manageStatus').classList.remove('bad');
  renderManage();
}

function flattenPlaylists(nodes, depth = 0, out = []) {
  for (const node of nodes || []) {
    out.push({ ...node, depth });
    flattenPlaylists(node.children, depth + 1, out);
  }
  return out;
}

function manageRow(target, entry) {
  const indent = entry.depth ? '&nbsp;'.repeat(entry.depth * 3) : '';
  const count = entry.count === undefined ? entry.tracks : entry.count;
  const actions = entry.folder ? '' :
    `<button class="btn tiny ghost" data-act="remove" title="Take the built set's tracks out of this playlist">Remove set</button>`;
  return `<div class="manage-row" data-id="${escapeHtml(String(entry.id))}">
      <span class="name" title="${escapeHtml(entry.name || '')}">${indent}${escapeHtml(entry.name || '')}` +
      `${entry.folder ? '<span class="muted"> · folder</span>' : ''}</span>
      <span class="num">${count === undefined ? '' : count}</span>
      <button class="btn tiny ghost" data-act="rename">Rename</button>
      ${actions}
      <button class="btn tiny ghost" data-act="delete">Delete</button>
    </div>`;
}

function renderManage() {
  const data = state.manage || {};
  const rxStatus = (data.rekordbox || {}).status || {};
  const ap = data.autopilot || {};

  $('#rxManageNote').textContent = rxStatus.blocked
    ? 'rekordbox is open — quit it to change anything'
    : (rxStatus.exists ? '' : 'no master.db found');
  $('#apManageNote').textContent = ap.blocked
    ? 'AUTO/PILOT is open — quit it to change anything'
    : (ap.exists ? `${ap.tracks} tracks` : 'no library found');

  const rxRows = flattenPlaylists((data.rekordbox || {}).playlists).map((e) => manageRow('rekordbox', e));
  const apRows = (ap.playlists || []).map((e) => manageRow('autopilot', e));
  $('#rxPlaylists').innerHTML = rxRows.join('') ||
    '<p class="empty">Load a library to list its playlists.</p>';
  $('#apPlaylists').innerHTML = apRows.join('') ||
    '<p class="empty">AUTO/PILOT has no playlists yet.</p>';
  $('#manageStatus').textContent = state.set.length
    ? `The built set has ${state.set.length} tracks`
    : 'Build a set to use “Remove set” and the delete buttons below';
}

function manageEntry(target, id) {
  const data = state.manage || {};
  if (target === 'autopilot') {
    return ((data.autopilot || {}).playlists || []).find((e) => String(e.id) === String(id));
  }
  return flattenPlaylists((data.rekordbox || {}).playlists)
    .find((e) => String(e.id) === String(id));
}

function setPending(pending) {
  state.pending = pending;
  $('#reportPending').textContent = `Dry run — ${pending.label}`;
  $('#reportCommit').hidden = false;
  $('#reportCancel').hidden = false;
}

function clearPending() {
  state.pending = null;
  $('#reportPending').textContent = '';
  $('#reportCommit').hidden = true;
  $('#reportCancel').hidden = true;
}

async function commitPending() {
  const pending = state.pending;
  if (!pending) return;
  try {
    const report = await api(pending.route, { ...pending.payload, dry_run: false });
    renderWriteReport([{ job: pending.job, report }], false);
    toast(`${pending.label} — done.`);
    state.meta = await api('/api/state');
    renderLibraryStatus();
    await refreshManage();
  } catch (error) {
    setPending(pending);
    toast(error.message, true);
  }
}

async function libraryAction(route, payload, job, label) {
  try {
    const report = await api(route, { ...payload, dry_run: true });
    renderWriteReport([{ job, report }], true);
    setPending({ route, payload, job, label });
  } catch (error) {
    toast(error.message, true);
  }
}

function manageClick(event, target) {
  const button = event.target.closest('button[data-act]');
  const row = event.target.closest('.manage-row');
  if (!row) return;
  const entry = manageEntry(target, row.getAttribute('data-id'));
  if (!entry) return;
  const action = button ? button.getAttribute('data-act') : null;
  if (action === 'delete') deletePlaylist(target, entry);
  else if (action === 'remove') removeSetFromPlaylist(target, entry);
  else if (action === 'rename') startRename(row, entry);
  else if (action === 'save') saveRename(row, target, entry);
  else if (action === 'cancel') renderManage();
}

function startRename(row, entry) {
  row.innerHTML = `<input class="text-input" value="${escapeHtml(entry.name || '')}" aria-label="New playlist name">` +
    '<button class="btn tiny primary" data-act="save">Save</button>' +
    '<button class="btn tiny ghost" data-act="cancel">Cancel</button>';
  const input = row.querySelector('input');
  input.focus();
  input.select();
}

function saveRename(row, target, entry) {
  const value = (row.querySelector('input').value || '').trim();
  if (!value || value === entry.name) { renderManage(); return; }
  libraryAction(
    '/api/library/playlist/rename',
    { target, id: entry.id, name: value },
    `${target} \u00b7 rename playlist`,
    `rename \u201c${entry.name}\u201d to \u201c${value}\u201d`
  );
}

function deletePlaylist(target, entry) {
  const count = entry.count === undefined ? entry.tracks : entry.count;
  libraryAction(
    '/api/library/playlist/delete',
    { target, id: entry.id },
    `${target} \u00b7 delete playlist`,
    `delete the ${target} playlist \u201c${entry.name}\u201d (${count} tracks)`
  );
}

function removeSetFromPlaylist(target, entry) {
  if (!state.set.length) { toast('Build a set first.', true); return; }
  libraryAction(
    '/api/library/playlist/remove-tracks',
    { target, id: entry.id, keys: state.set.map((t) => t.key) },
    `${target} \u00b7 remove set`,
    `remove the set's ${state.set.length} tracks from \u201c${entry.name}\u201d`
  );
}

function deleteSetTracks(target) {
  if (!state.set.length) { toast('Build a set first.', true); return; }
  libraryAction(
    '/api/library/tracks/delete',
    { target, keys: state.set.map((t) => t.key) },
    `${target} \u00b7 delete tracks`,
    `delete the set's ${state.set.length} tracks from ${target} (the files stay on disk)`
  );
}

/* ------------------------------------------------------ rename & convert */

/*
 * A downloads folder is where a library starts its life, and it arrives named
 * for a web page: emoji, "[Official Video]", underscores, a bitrate, and half of
 * it in a format that carries encoder padding.  This panel reads the folder and
 * reports what each file would be called and what it would become, then
 * converts on a second click -- the same dry run then commit shape as a
 * database write, because the two are equally hard to undo by hand.
 */

/*
 * Choosing the folder.
 *
 * A web page is never told the real path of the folder the user picked: an
 * <input type="file" webkitdirectory> reports relative names and a fake root,
 * and nothing else in the page can look at the disk.  The app window can hand
 * it over, because its WKWebView asks us instead of showing a panel of its own:
 * main.swift opens a native choose-a-folder sheet and calls parityPickedPath
 * below with the path.  The buttons are therefore only offered where that
 * bridge exists -- the app window -- and a browser types the path instead.
 */
const PICK_FIELDS = {
  source: '#ingestSource',
  destination: '#ingestDestination',
  watch: '#watchPath'
};

function pickFolder(who) {
  const field = $(PICK_FIELDS[who]);
  // Read by the app before it opens the sheet, so "Choose…" starts where the
  // field already points, and so the path comes back to the right one.
  window.__parityPickTarget = who;
  window.__parityPickStart = field.value.trim();
  $('#folderPick').click();
}

/* Called by the app (main.swift) with the folder the user chose. */
window.parityPickedPath = (who, path) => {
  window.__parityPickTarget = '';
  window.__parityPickStart = '';
  const field = $(PICK_FIELDS[who]);
  if (!field || !path) return;
  field.value = path;
  // The same as typing it: whatever was scanned no longer matches the field.
  field.dispatchEvent(new Event('input', { bubbles: true }));
};

function syncPickButtons() {
  const bridged = isAppShell();
  Object.values(PICK_FIELDS).forEach((selector) => {
    $(`${selector}Pick`).hidden = !bridged;
  });
}

async function toggleIngest() {
  const panel = $('#ingestPanel');
  panel.hidden = !panel.hidden;
  if (!panel.hidden) {
    $('#managePanel').hidden = true;
    $('#watchPanel').hidden = true;
    closeCueEditor();
    await refreshIngest();
  }
}

/*
 * The watch folder and M3U import.
 *
 * A new download should not need a trip through rekordbox to become a track the
 * pool can build with.  The watch folder reads a folder every few seconds and
 * adds what landed; an M3U is the same import, once, from a file.  Both add
 * library rows and a playlist -- nothing is moved, converted or written to the
 * DJ apps.
 */
async function toggleWatch() {
  const panel = $('#watchPanel');
  panel.hidden = !panel.hidden;
  if (!panel.hidden) {
    $('#managePanel').hidden = true;
    $('#ingestPanel').hidden = true;
    closeCueEditor();
    await refreshWatch();
  }
}

async function refreshWatch() {
  const status = $('#watchStatus');
  status.textContent = 'Reading the watch folder…';
  status.classList.remove('bad');
  try {
    state.watch = await api('/api/watch');
  } catch (error) {
    state.watch = null;
    status.textContent = error.message;
    status.classList.add('bad');
    return;
  }
  renderWatch();
}

function renderWatch() {
  const info = state.watch || {};
  $('#watchPath').value = info.path || '';
  $('#watchPlaylist').value = info.playlist || '';
  $('#watchRecursive').checked = info.recursive !== false;
  $('#watchEnabled').checked = Boolean(info.enabled);
  const status = $('#watchStatus');
  status.textContent = info.enabled
    ? `${info.running ? 'watching' : 'on'} · ${info.playlist_count || 0} track(s) in “${info.playlist || 'the playlist'}”`
    : 'off — switching it on imports new files as they land';
  status.title = info.last_scan
    ? `last scan ${info.last_scan}, ${info.last_added || 0} added; ${info.seen || 0} file(s) remembered`
    : 'No scan yet.';
  if (info.error) status.title = info.error;
  const parts = [];
  if (info.path) {
    parts.push(info.exists
      ? `Folder: ${escapeHtml(info.path)}`
      : `<span class="warn">No folder at ${escapeHtml(info.path)}</span>`);
  }
  if (info.available === false) {
    parts.push('<span class="warn">ffprobe is missing: files still import, but their tags and length cannot be read.</span>');
  }
  if (info.last_scan) {
    parts.push(`Last scan ${escapeHtml(info.last_scan)}: ${info.last_added || 0} new file(s), `
      + `${info.seen || 0} remembered.`);
  }
  if (info.last_error) parts.push(`<span class="warn">${escapeHtml(info.last_error)}</span>`);
  $('#watchReport').innerHTML = parts.length ? parts.join('<br>') : 'Pick a folder to watch, or import an M3U below.';
}

function watchReportHtml(report) {
  if (!report) return '';
  const rows = (report.added_tracks || []).slice(0, 20).map((track) => (
    `<tr><td>${escapeHtml(track.label || '')}</td><td class="muted">${escapeHtml(track.path || '')}</td></tr>`
  )).join('');
  return `<div>${report.scanned || 0} file(s) scanned, <b>${report.added || 0}</b> new`
    + `${report.playlist ? ` into “${escapeHtml(report.playlist)}”` : ''}.</div>`
    + (report.note ? `<div class="warn">${escapeHtml(report.note)}</div>` : '')
    + (rows ? `<table>${rows}</table>` : '');
}

async function saveWatch() {
  const path = $('#watchPath').value.trim();
  if (!path) { toast('Choose a folder to watch first.', true); return; }
  try {
    const result = await api('/api/watch', {
      path,
      enabled: $('#watchEnabled').checked,
      recursive: $('#watchRecursive').checked,
      playlist: $('#watchPlaylist').value.trim()
    });
    state.watch = result.status;
    state.meta = result.state;
    renderWatch();
    $('#watchReport').innerHTML = watchReportHtml(result.report);
    toast(result.report && result.report.added
      ? `Imported ${result.report.added} new file(s) into “${result.report.playlist}”.`
      : ($('#watchEnabled').checked ? 'Watching that folder.' : 'Watch folder saved.'));
    await loadState();
  } catch (error) {
    toast(error.message, true);
  }
}

async function scanWatch() {
  try {
    const result = await api('/api/watch/tick', {});
    state.watch = result.status;
    state.meta = result.state;
    renderWatch();
    $('#watchReport').innerHTML = watchReportHtml(result.report);
    toast(result.report.added
      ? `Imported ${result.report.added} new file(s).`
      : 'No new audio files in that folder.');
    await loadState();
  } catch (error) {
    toast(error.message, true);
  }
}

async function importM3UFile(file) {
  try {
    const text = await file.text();
    const result = await api('/api/import/m3u', { text, name: file.name.replace(/\.[^.]+$/, '') });
    const missing = (result.missing || []).length;
    toast(`Imported “${result.playlist}”: ${result.added} new track(s), `
      + `${result.matched} already in the library`
      + (missing ? `, ${missing} file(s) not found` : ''));
    state.meta = result.state;
    await loadState();
    $('#watchPanel').hidden = false;
    await refreshWatch();
    if (missing) {
      $('#watchReport').innerHTML = `<div class="warn">${missing} path(s) in ${escapeHtml(file.name)} do not exist; `
        + 'the tracks that do were imported.</div>';
    }
  } catch (error) {
    toast(error.message, true);
  }
}

async function refreshIngest() {
  const status = $('#ingestStatus');
  status.textContent = 'Checking ffmpeg…';
  status.classList.remove('bad');
  try {
    state.ingest = await api('/api/ingest');
  } catch (error) {
    state.ingest = null;
    status.textContent = error.message;
    status.classList.add('bad');
    return;
  }
  renderIngestStatus();
  syncIngestForm();
}

function renderIngestStatus() {
  const node = $('#ingestStatus');
  const ingest = state.ingest || {};
  const formats = (ingest.formats || []).map((format) => format.label).join(' and ');
  node.classList.toggle('bad', !ingest.available);
  if (!ingest.available) {
    node.textContent = ingest.error || 'converting needs ffmpeg';
    node.title = ingest.install_hint || '';
    return;
  }
  // Without ffprobe the names are still fixed and the plan still works; only
  // the length, tags and silence measurements go missing.
  node.textContent = ingest.ffprobe
    ? `ffmpeg ready — ${formats}`
    : `ffmpeg ready — ${formats}, but ffprobe is missing so tags and silence are not measured`;
}

function syncIngestForm() {
  const ingest = state.ingest || {};
  const settings = ingest.settings || {};
  const select = $('#ingestFormat');
  // The two formats are in the markup, so the list always has text; the API is
  // only allowed to extend it, never to leave it empty.
  const formats = ingest.formats || [];
  if (formats.length) {
    select.innerHTML = formats
      .map((format) => `<option value="${escapeHtml(format.key)}">${escapeHtml(format.label)}</option>`)
      .join('');
  } else if (!select.options.length) {
    select.innerHTML = '<option value="aiff">AIFF</option><option value="wav">WAV</option>';
  }
  if (settings.format) select.value = settings.format;
  $('#ingestSource').value = settings.source || '';
  $('#ingestDestination').value = settings.destination || '';
  $('#ingestSource').placeholder = 'the folder your downloads land in';
  const boxes = {
    recursive: '#ingestRecursive',
    measure: '#ingestMeasure',
    trim_silence: '#ingestTrim',
    overwrite: '#ingestOverwrite'
  };
  Object.entries(boxes).forEach(([key, selector]) => {
    if (typeof settings[key] === 'boolean') $(selector).checked = settings[key];
  });
}

function ingestPayload(extra = {}) {
  return {
    source: $('#ingestSource').value.trim(),
    destination: $('#ingestDestination').value.trim(),
    format: $('#ingestFormat').value || 'aiff',
    recursive: $('#ingestRecursive').checked,
    measure: $('#ingestMeasure').checked,
    trim_silence: $('#ingestTrim').checked,
    overwrite: $('#ingestOverwrite').checked,
    ...extra
  };
}

function ingestBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)} GB`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(value / 1e3))} kB`;
}

async function ingestScan() {
  const button = $('#ingestScanBtn');
  if (!$('#ingestSource').value.trim()) {
    toast('Type the folder to read first.', true);
    return;
  }
  button.disabled = true;
  $('#ingestRunBtn').hidden = true;
  $('#ingestSummary').textContent = 'Reading the folder…';
  try {
    const data = await api('/api/ingest', ingestPayload());
    state.ingest = data.status || state.ingest;
    state.ingestPlan = data.plan;
    renderIngestStatus();
    renderIngestScan(data.plan, data.report);
  } catch (error) {
    $('#ingestSummary').textContent = '';
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function ingestRun() {
  const button = $('#ingestRunBtn');
  const plan = state.ingestPlan || { summary: {} };
  button.disabled = true;
  button.textContent = 'Converting…';
  $('#ingestSummary').textContent = `${plan.summary.converts || 0} to convert, ${plan.summary.copies || 0} to copy`;
  try {
    const data = await api('/api/ingest/run', ingestPayload());
    state.ingest = data.status || state.ingest;
    state.ingestPlan = data.plan;
    renderIngestStatus();
    renderIngestDone(data.plan, data.report);
    const summary = data.report.summary || {};
    toast(`${summary.converted} converted, ${summary.copied} copied into ${summary.folder}`
      + (summary.failed ? ` — ${summary.failed} failed` : ''), Boolean(summary.failed));
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = 'Convert';
  }
}

function ingestHead(plan, title) {
  const summary = plan.summary || {};
  const parts = [`<b>${summary.found || 0}</b> file${summary.found === 1 ? '' : 's'}`];
  if (summary.renames) parts.push(`${summary.renames} to rename`);
  if (summary.converts) parts.push(`${summary.converts} to convert to ${String(plan.format).toUpperCase()}`);
  if (summary.copies) parts.push(`${summary.copies} kept as ${String(plan.format).toUpperCase()}`);
  if (summary.exists) parts.push(`${summary.exists} already there`);
  return `<h4>${escapeHtml(title)}</h4><div>${parts.join(' · ')}</div>`
    + `<div class="num">${escapeHtml(plan.destination || '')}</div>`
    + (plan.notes || []).map((note) => `<div class="num">${escapeHtml(note)}</div>`).join('');
}

function ingestRows(plan) {
  return (plan.items || []).map((item) => {
    const changes = (item.changes || []).map((change) => `${change.kind}: ${change.detail}`).join('\n');
    const padding = item.padding;
    const lead = padding && padding.leading_ms ? Number(padding.leading_ms) : 0;
    const padText = lead
      ? `${lead.toFixed(0)} ms lead${padding.complete ? '' : ' (first 20 s)'}`
      : '';
    const name = item.name_changes
      ? `<span class="path">${escapeHtml(item.relative)}</span>`
        + `<span class="arrow"> → </span><b>${escapeHtml(item.name)}</b>`
      : `<span class="path">${escapeHtml(item.relative)}</span>`;
    return `<tr title="${escapeHtml(changes)}">`
      + `<td class="num" style="text-align:left">${escapeHtml(item.action)}</td>`
      + `<td>${name}</td>`
      + `<td class="num">${ingestBytes(item.size)}</td>`
      + `<td class="pad ${padText ? '' : 'none'}">${escapeHtml(padText || '—')}</td></tr>`;
  }).join('');
}

function renderIngestScan(plan, report) {
  const skipped = (report.skipped || []).length;
  $('#ingestReport').innerHTML = ingestHead(plan, 'rename & convert — dry run')
    + `<table>${ingestRows(plan)}</table>`
    + (skipped ? `<div class="num">${skipped} file(s) would be skipped as they are already in the destination.</div>` : '')
    + '<div class="num">Nothing has been written. Convert writes the tidied files and leaves the originals alone.</div>';
  const actionable = (plan.summary || {}).ready || 0;
  const button = $('#ingestRunBtn');
  button.hidden = !actionable;
  button.textContent = `Convert ${actionable} file${actionable === 1 ? '' : 's'}`;
  $('#ingestSummary').textContent = actionable
    ? 'Check the names above, then convert'
    : 'Nothing to do in that folder';
}

function renderIngestDone(plan, report) {
  const summary = report.summary || {};
  const rows = (report.written || []).concat(report.copied || []);
  const detail = rows.map((row) => {
    const padding = row.padding;
    const lead = padding ? `${Number(padding.leading_ms).toFixed(0)} ms` : '—';
    const tail = padding && padding.trailing_ms ? `${Number(padding.trailing_ms).toFixed(0)} ms` : '—';
    const lost = ((row.tags || {}).lost || []).length;
    const tags = row.tags && row.tags.error
      ? '<span class="warn">tags not read back</span>'
      : (lost ? `<span class="warn">lost: ${escapeHtml(row.tags.lost.join(', '))}</span>` : `${(row.tags || {}).after ? row.tags.after.length : 0} tags kept`);
    return `<tr>`
      + `<td>${escapeHtml(row.name)}</td>`
      + `<td class="num">${ingestBytes(row.bytes)}</td>`
      + `<td class="num">${escapeHtml(lead)}</td>`
      + `<td class="num">${escapeHtml(tail)}</td>`
      + `<td>${tags}${row.cover_dropped ? ' · <span class="num">cover art not carried over</span>' : ''}`
      + `${row.trimmed_ms ? ` · <span class="num">trimmed ${Number(row.trimmed_ms).toFixed(0)} ms</span>` : ''}</td></tr>`;
  }).join('');

  $('#ingestReport').innerHTML = ingestHead(plan, 'rename & convert — written')
    + `<div><b>${summary.converted}</b> converted · <b>${summary.copied}</b> copied · `
    + `${summary.skipped} skipped · ${summary.failed} failed · ${report.elapsed}s</div>`
    + (rows.length ? '<table>' + detail + '</table>' : '')
    + '<div class="num">The silence column is what the converted file starts and ends with: an MP3 that carried 200 ms of lead padding plays 200 ms later than its cue points suggest.</div>'
    + (report.failed || []).map((row) =>
      `<div class="warn">${escapeHtml(row.source.split('/').pop())}: ${escapeHtml(row.reason)}</div>`).join('')
    + (report.skipped || []).slice(0, 12).map((row) =>
      `<div class="num">skipped ${escapeHtml(String(row.source).split('/').pop())} — ${escapeHtml(row.reason || 'already there')}</div>`).join('');
  $('#ingestRunBtn').hidden = true;
  $('#ingestSummary').textContent = `Written to ${summary.folder}`;
}

/* ------------------------------------------------------------ cue editor */

/*
 * The waveform, and the cues that sit on it.
 *
 * The picture arrives already reduced to peaks (see parity/waveform.py):
 * three bands of 0-255, so the page never touches PCM and a six-minute track is
 * a few thousand bytes.  Everything drawn here -- the bar grid, the mix-in and
 * mix-out windows, the cue flags -- is placed on one time axis, the track's own
 * length, which is what makes a cue's position mean the same thing on screen as
 * it does in the database.
 *
 * A drag rewrites the cue in the stored plan, and moving a cue locks it:
 * "Analyse cues" rebuilds every position from rekordbox's phrase map, and the
 * one position it must not quietly undo is the one somebody chose by hand.
 */
const CUE_BAND_STYLE = [
  // Below 250 Hz: the kick and the bassline, the body of the waveform.
  { key: 'low', rgb: '63,183,240', alpha: 0.85 },
  // 250 Hz - 4 kHz: vocals and synths.
  { key: 'mid', rgb: '79,208,122', alpha: 0.8 },
  // 4 kHz and up: hats and snares.
  { key: 'high', rgb: '232,237,247', alpha: 0.7 }
];
const CUE_SNAP_FRACTION = { sixteenth: 0.25, beat: 1, bar: 4 };
const CUE_CANVAS_HEIGHT = 230;
const CUE_ZONE_HEIGHT = 20;
const CUE_FLAG_HEIGHT = 16;
const CUE_ZOOMS = [1, 2, 4, 8, 16];

const cueEditor = {
  track: null,
  plan: null,
  wave: null,
  zoom: 1,
  start: 0,      // milliseconds at the left edge of the view
  drag: null,    // { cue, ms, was, dx, dy, moved, mode }
  error: '',
  raf: 0,
  playhead: -1
};

function cueAxisMs() {
  const plan = Number((cueEditor.plan || {}).duration_ms) || 0;
  const wave = Number((cueEditor.wave || {}).duration_ms) || 0;
  const track = Math.round((Number(cueEditor.track && cueEditor.track.duration) || 0) * 1000);
  return Math.max(1000, plan, wave, track);
}

/* How much audio the peaks actually describe: a truncated profile covers less
   than the track, and stretching it over the whole axis would move every cue. */
function cuePeaksMs() {
  return Number((cueEditor.wave || {}).duration_ms) || cueAxisMs();
}

function cueBpm() {
  const plan = Number((cueEditor.plan || {}).bpm) || 0;
  return plan || Number(cueEditor.track && cueEditor.track.bpm) || 0;
}

function cueSnapStep() {
  const mode = $('#cueSnap').value;
  const bpm = cueBpm();
  if (mode === 'off' || !bpm) return 0;
  const beat = 60000 / bpm;
  return beat * (CUE_SNAP_FRACTION[mode] || 0.25);
}

function cueSnap(ms, free) {
  const step = free ? 0 : cueSnapStep();
  if (!step) return Math.round(ms);
  const base = Number((cueEditor.plan || {}).first_beat_ms) || 0;
  return Math.round((ms - base) / step) * step + base;
}

/*
 * One description of the canvas, shared by everything that draws on it and
 * everything that reads the pointer off it.
 *
 * ``width`` is the width the canvas is *displayed* at, not the width of its
 * backing store: the two used to differ by the wrapper's padding, and a pointer
 * measured against the wrong one lands a little to the left of where the user
 * clicked.  ``scale`` is the backing store per displayed pixel, so the drawing
 * fits the element exactly even at a fractional device pixel ratio.
 */
function cueGeometry() {
  const canvas = $('#cueCanvas');
  const rect = canvas.getBoundingClientRect();
  const width = rect.width || canvas.width / (window.devicePixelRatio || 1);
  const scale = width ? canvas.width / width : 1;
  const axis = cueAxisMs();
  const span = axis / cueEditor.zoom;
  const start = Math.min(Math.max(0, cueEditor.start), Math.max(0, axis - span));
  const x = (ms) => ((ms - start) / span) * width;
  return {
    canvas, rect, width, scale, height: canvas.height / scale, axis, span, start,
    x,
    ms: (px) => start + (px / width) * span,
    visible: (ms) => ms >= start - span && ms <= start + span * 2
  };
}

function cueColour(cue) {
  const rgb = Array.isArray(cue.rgb) && cue.rgb.length === 3 ? cue.rgb : null;
  // The exact colour the write puts in rekordbox's palette, so the pin on screen
  // and the pad on the CDJ are the same colour.
  return rgb ? `rgb(${rgb[0]},${rgb[1]},${rgb[2]})` : '#8b95a7';
}

/* The canvas's own box, not its wrapper's: the wrapper carries the padding, and
   measuring it sized the backing store 24px wider than the element it is drawn
   on, which squashed every drawn feature and shifted a click off the pointer. */
function sizeCueCanvas() {
  const canvas = $('#cueCanvas');
  const dpr = window.devicePixelRatio || 1;
  const css = Math.max(320, Math.round(canvas.getBoundingClientRect().width));
  canvas.width = Math.round(css * dpr);
  canvas.height = Math.round(CUE_CANVAS_HEIGHT * dpr);
  canvas.style.height = `${CUE_CANVAS_HEIGHT}px`;
}

function drawCueEditor() {
  if ($('#cuePanel').hidden || !cueEditor.track) return;
  const canvas = $('#cueCanvas');
  const context = canvas.getContext('2d');
  const geom = cueGeometry();
  const { width, height, axis } = geom;
  context.setTransform(geom.scale, 0, 0, geom.scale, 0, 0);
  context.clearRect(0, 0, width, height);
  context.fillStyle = '#0b0d12';
  context.fillRect(0, 0, width, height);

  const waveTop = 0;
  const waveBottom = height - CUE_ZONE_HEIGHT;
  const centre = waveTop + (waveBottom - waveTop) / 2;
  const half = (waveBottom - waveTop) / 2 - CUE_FLAG_HEIGHT - 3;

  drawCueGrid(context, geom, waveTop, waveBottom);
  drawCueBands(context, geom, centre, half);
  drawCueZones(context, geom, waveBottom, height);
  (cueEditor.plan ? cueEditor.plan.hot_cues || [] : []).forEach((cue) => {
    drawCuePin(context, geom, cue, waveTop, waveBottom, centre);
  });
  // Last, so a cue flag underneath cannot hide where the playback is.
  drawCuePlayhead(context, geom, waveTop, waveBottom);
  if (!cueEditor.plan) {
    context.fillStyle = 'rgba(231,235,243,.75)';
    context.font = '12px ui-monospace, SFMono-Regular, Menlo, monospace';
    context.fillText('no cue plan for this track yet', 12, centre);
  }
}

/* Bars, and the phrase lines they gather into: thin every 8, brighter every 32. */
function drawCueGrid(context, geom, top, bottom) {
  const bpm = cueBpm();
  if (!bpm) return;
  const barMs = (60000 / bpm) * 4;
  const base = Number((cueEditor.plan || {}).first_beat_ms) || 0;
  const steps = [
    { bars: 8, style: 'rgba(255,255,255,.05)', width: 1 },
    { bars: 16, style: 'rgba(255,255,255,.08)', width: 1 },
    { bars: 32, style: 'rgba(255,255,255,.16)', width: 1 }
  ];
  context.save();
  steps.forEach((step) => {
    const every = barMs * step.bars;
    const px = (every / geom.span) * geom.width;
    // Too many lines is a grey wash: a grid that cannot be counted is noise.
    if (px < 7) return;
    context.strokeStyle = step.style;
    context.lineWidth = step.width;
    context.beginPath();
    for (let ms = base; ms <= base + geom.axis; ms += every) {
      if (!geom.visible(ms)) continue;
      const x = Math.round(geom.x(ms)) + 0.5;
      context.moveTo(x, top);
      context.lineTo(x, bottom);
    }
    context.stroke();
  });
  context.restore();
}

/*
 * The three bands, mirrored around the centre line.  Each column takes the
 * loudest peak inside it, so zooming out keeps the transients instead of
 * averaging them away, and each band is drawn over the one below it with the
 * bass widest and the hats tightest -- which is how a kick, a vocal and a
 * hi-hat stay three different shapes on one picture.
 */
function drawCueBands(context, geom, centre, half) {
  const wave = cueEditor.wave;
  if (!wave || !wave.peaks) return;
  const span = cuePeaksMs();
  const left = Math.max(0, Math.floor(geom.x(0)));
  const right = Math.min(geom.width, Math.ceil(geom.x(span)));
  if (right <= left) return;
  context.save();
  CUE_BAND_STYLE.forEach((band) => {
    const peaks = wave.peaks[band.key] || [];
    if (!peaks.length) return;
    const top = [];
    const bottom = [];
    for (let px = left; px <= right; px += 1) {
      const from = Math.floor((geom.ms(px) / span) * peaks.length);
      const to = Math.ceil((geom.ms(px + 1) / span) * peaks.length);
      let value = 0;
      for (let index = Math.max(0, from); index < Math.min(peaks.length, Math.max(to, from + 1)); index += 1) {
        value = Math.max(value, peaks[index]);
      }
      const height = (value / 255) * half;
      top.push([px, centre - height]);
      bottom.push([px, centre + height]);
    }
    context.beginPath();
    top.forEach(([px, y], index) => (index ? context.lineTo(px, y) : context.moveTo(px, y)));
    bottom.reverse().forEach(([px, y]) => context.lineTo(px, y));
    context.closePath();
    context.fillStyle = `rgba(${band.rgb},${band.alpha})`;
    context.fill();
  });
  context.restore();
}

/*
 * The mixing windows, as striped bands along the bottom: where this track can
 * be mixed into (its intro, up to the Intro End cue) and where it wants to be
 * mixed out of (the closing bars from Mix-Out).  Two tracks side by side whose
 * stripes do not overlap cannot be blended without cutting one of them short,
 * which is worth seeing before the export rather than in the booth.
 */
function drawCueZones(context, geom, top, bottom) {
  const plan = cueEditor.plan;
  if (!plan) return;
  const at = (slot) => {
    const cue = (plan.hot_cues || []).find((item) => item.slot === slot);
    return cue ? cue.ms : null;
  };
  const mixIn = at('B');
  const mixOut = at('H');
  if (mixIn !== null) drawCueZone(context, geom, 0, mixIn, top, bottom, '79,208,122', 'MIX IN');
  if (mixOut !== null) {
    const end = Math.max(mixOut, cuePeaksMs());
    drawCueZone(context, geom, mixOut, end, top, bottom, '255,139,107', 'MIX OUT');
  }
}

function drawCueZone(context, geom, fromMs, toMs, top, bottom, rgb, label) {
  const x0 = Math.max(-2, geom.x(fromMs));
  const x1 = Math.min(geom.width + 2, geom.x(toMs));
  if (x1 - x0 < 2) return;
  context.save();
  context.beginPath();
  context.rect(x0, top, x1 - x0, bottom - top);
  context.clip();
  context.fillStyle = `rgba(${rgb},.13)`;
  context.fillRect(x0, top, x1 - x0, bottom - top);
  context.strokeStyle = `rgba(${rgb},.5)`;
  context.lineWidth = 1;
  context.beginPath();
  const height = bottom - top;
  for (let x = x0 - height; x < x1 + height; x += 8) {
    context.moveTo(x, bottom);
    context.lineTo(x + height, top);
  }
  context.stroke();
  context.restore();
  if (x1 - x0 > 58) {
    context.save();
    context.fillStyle = `rgba(${rgb},.85)`;
    context.font = 'bold 9px ui-monospace, SFMono-Regular, Menlo, monospace';
    context.fillText(label, x0 + 5, bottom - 5);
    context.restore();
  }
}

function drawCuePlayhead(context, geom, top, bottom) {
  if (!player.track || !cueEditor.track || player.track.key !== cueEditor.track.key) return;
  const ms = (player.el.currentTime || 0) * 1000;
  if (!geom.visible(ms)) return;
  const x = Math.round(geom.x(ms)) + 0.5;
  context.save();
  context.strokeStyle = 'rgba(255,255,255,.95)';
  context.lineWidth = 1.5;
  context.beginPath();
  context.moveTo(x, top);
  context.lineTo(x, bottom);
  context.stroke();
  context.restore();
}

function drawCuePin(context, geom, cue, top, bottom, centre) {
  // A drag is either about a cue or a pan, and a pan carries no cue at all.
  // Reading ``drag.cue.slot`` without checking threw part-way through a redraw,
  // which left the canvas cleared with no flags on it until the panel was
  // reopened -- so pressing on the waveform made the cues disappear.
  const drag = cueEditor.drag;
  const dragging = !!drag && !!drag.cue && drag.cue.slot === cue.slot;
  const ms = dragging ? drag.ms : cue.ms;
  if (!geom.visible(ms)) return;
  const x = geom.x(ms);
  const colour = cueColour(cue);
  context.save();
  context.strokeStyle = colour;
  context.lineWidth = cue.locked ? 2.5 : 1.5;
  context.beginPath();
  context.moveTo(Math.round(x) + 0.5, top);
  context.lineTo(Math.round(x) + 0.5, bottom);
  context.stroke();

  // The flag: the pad letter, and a lock once the position is the user's.
  const width = 15 + (cue.locked ? 9 : 0);
  const left = Math.min(Math.max(0, x - 7), Math.max(0, geom.width - width));
  context.fillStyle = colour;
  context.globalAlpha = dragging ? 1 : 0.92;
  context.beginPath();
  context.roundRect(left, top + 1, width, CUE_FLAG_HEIGHT, 3);
  context.fill();
  context.globalAlpha = 1;
  context.fillStyle = '#10141b';
  context.font = 'bold 10px ui-monospace, SFMono-Regular, Menlo, monospace';
  context.textBaseline = 'middle';
  context.fillText(cue.slot || '?', left + 4, top + 1 + CUE_FLAG_HEIGHT / 2);
  if (cue.locked) {
    context.font = '9px system-ui, sans-serif';
    context.fillText('🔒', left + width - 9, top + 1 + CUE_FLAG_HEIGHT / 2);
  }
  if (dragging) {
    context.fillStyle = 'rgba(11,13,18,.85)';
    context.fillRect(left - 30, top + CUE_FLAG_HEIGHT + 4, 74, 14);
    context.fillStyle = '#e7ebf3';
    context.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace';
    context.fillText(fmtClock(ms / 1000), left - 26, top + CUE_FLAG_HEIGHT + 11);
  }
  context.restore();
  context.textBaseline = 'alphabetic';
}

/* ------------------------------------------------------ opening and data */

async function openCueEditor(track) {
  if (!track) return;
  cueEditor.track = track;
  cueEditor.plan = null;
  cueEditor.wave = null;
  cueEditor.error = '';
  cueEditor.drag = null;
  cueEditor.playhead = -1;
  cueEditor.zoom = Number($('#cueZoom').value) || 1;
  cueEditor.start = 0;
  $('#cuePanel').hidden = false;
  $('#managePanel').hidden = true;
  $('#ingestPanel').hidden = true;
  $('#cueTitle').innerHTML = escapeHtml(trackLabel(track));
  setCueStatus('Reading the waveform…');
  $('#cueReadout').textContent = '';
  sizeCueCanvas();
  drawCueEditor();

  const [plan, wave] = await Promise.allSettled([
    api(`/api/cues?key=${encodeURIComponent(track.key)}`),
    api(`/api/waveform?key=${encodeURIComponent(track.key)}`)
  ]);
  if (cueEditor.track !== track) return;   // another track was opened meanwhile
  const problems = [];
  if (plan.status === 'fulfilled') cueEditor.plan = plan.value;
  else problems.push(plan.reason.message);
  if (wave.status === 'fulfilled') cueEditor.wave = wave.value.waveform;
  else problems.push(wave.reason.message);
  cueEditor.error = problems.join(' · ');
  if (problems.length && !cueEditor.wave) setCueStatus(problems[0], true);
  else setCueStatus();
  drawCueEditor();
  startCuePlayhead();
}

function setCueStatus(message = '', bad = false) {
  const node = $('#cueStatus');
  node.classList.toggle('bad', !!bad);
  if (message) { node.textContent = message; return; }
  const parts = [];
  if (cueEditor.wave) {
    parts.push(`${fmtClock(cueEditor.wave.seconds)} of audio`);
    parts.push(`${cueEditor.wave.peaks_per_band} peaks/band`);
    parts.push(cueEditor.wave.engine === 'librosa' ? 'librosa' : 'numpy');
    if (cueEditor.wave.cached) parts.push('cached');
    if (cueEditor.wave.truncated) parts.push('first 30 minutes only');
  }
  const bump = cueEditor.error;
  if (bump && cueEditor.wave) parts.push(bump);
  node.textContent = parts.join(' · ');
  node.classList.toggle('bad', !!bump);
}

function closeCueEditor() {
  $('#cuePanel').hidden = true;
  cueEditor.track = null;
  cueEditor.drag = null;
  stopCuePlayhead();
}

/*
 * The playhead lives on the canvas, so a redraw per animation frame is what
 * makes it travel across the track instead of stepping four times a second.
 *
 * The loop runs for as long as the panel is open.  It used to stop the first
 * time it saw a paused player -- which is every panel opened on a track that is
 * not playing yet -- so playing that track afterwards drew no playhead at all.
 * It redraws only when the position has moved, so an idle panel costs one
 * comparison per frame and nothing else.
 */
function startCuePlayhead() {
  stopCuePlayhead();
  const step = () => {
    cueEditor.raf = 0;
    if ($('#cuePanel').hidden || !cueEditor.track || !player.track
        || player.track.key !== cueEditor.track.key) return;
    const ms = (player.el.currentTime || 0) * 1000;
    if (Math.abs(ms - cueEditor.playhead) > 4) {
      cueEditor.playhead = ms;
      drawCueEditor();
    }
    cueEditor.raf = window.requestAnimationFrame(step);
  };
  cueEditor.raf = window.requestAnimationFrame(step);
}

function stopCuePlayhead() {
  if (cueEditor.raf) window.cancelAnimationFrame(cueEditor.raf);
  cueEditor.raf = 0;
}

/* --------------------------------------------------------------- dragging */

function cueAt(geom, event) {
  const plan = cueEditor.plan;
  if (!plan) return null;
  const px = event.clientX - geom.rect.left;
  const py = event.clientY - geom.rect.top;
  const dragging = cueEditor.drag && cueEditor.drag.cue ? cueEditor.drag.cue.slot : null;
  const flagHit = plan.hot_cues.filter((cue) => {
    const x = geom.x(dragging === cue.slot ? cueEditor.drag.ms : cue.ms);
    const width = 15 + (cue.locked ? 9 : 0);
    const left = Math.min(Math.max(0, x - 7), Math.max(0, geom.width - width));
    return px >= left - 3 && px <= left + width + 3 && py <= CUE_FLAG_HEIGHT + 5;
  });
  if (flagHit.length) return { cue: flagHit[flagHit.length - 1], mode: 'flag' };
  const lineHit = plan.hot_cues
    .map((cue) => ({ cue, distance: Math.abs(geom.x(cue.ms) - px) }))
    .filter((item) => item.distance <= 5)
    .sort((a, b) => a.distance - b.distance)[0];
  return lineHit ? { cue: lineHit.cue, mode: 'line' } : null;
}

function bindCueEditor() {
  const canvas = $('#cueCanvas');
  $('#cueClose').addEventListener('click', closeCueEditor);
  $('#cueZoom').addEventListener('change', () => {
    cueEditor.zoom = Number($('#cueZoom').value) || 1;
    // Keep the playhead, or the middle of the view, where the eye was.
    const anchor = cueEditor.playhead > 0 ? cueEditor.playhead : cueEditor.start + cueAxisMs() / (cueEditor.zoom * 2);
    centreCueView(anchor);
    drawCueEditor();
  });
  $('#cueSnap').addEventListener('change', () => {
    const mode = $('#cueSnap').value;
    $('#cueReadout').textContent = mode === 'off' ? 'snap off' : `snap ${$('#cueSnap').selectedOptions[0].textContent}`;
    drawCueEditor();
  });

  canvas.addEventListener('pointerdown', (event) => {
    if (!cueEditor.track || !cueEditor.plan) return;
    const geom = cueGeometry();
    const hit = cueAt(geom, event);
    // Capture keeps the drag alive outside the canvas. It throws when there is no
    // such pointer to capture -- a released pointer, or a synthetic click from a
    // test -- and the gesture works without it, so it is not worth failing over.
    try { canvas.setPointerCapture(event.pointerId); } catch { /* drag without it */ }
    // The point in the track under the pointer, taken once. A release without a
    // drag seeks here. The pan branch used to carry ``geom.axis`` instead -- the
    // length of the whole track -- so clicking anywhere on the waveform without
    // landing on a cue jumped straight to the end of the song.
    const at = geom.ms(event.clientX - geom.rect.left);
    if (hit) {
      cueEditor.drag = {
        cue: hit.cue, mode: hit.mode, ms: hit.cue.ms, was: hit.cue.ms,
        moved: false, pointer: event.pointerId, at
      };
      canvas.classList.add('moving');
    } else {
      cueEditor.drag = {
        mode: 'pan', ms: 0, was: 0, moved: false, pointer: event.pointerId,
        px: event.clientX, start: cueEditor.start, at,
        cue: null   // a pan is not about any cue; nothing draws it as dragged
      };
      canvas.classList.add('grabbing');
    }
    event.preventDefault();
  });

  canvas.addEventListener('pointermove', (event) => {
    const drag = cueEditor.drag;
    if (!drag) { hoverCueCursor(event); return; }
    if (drag.mode === 'pan') {
      const geom = cueGeometry();
      const dx = event.clientX - drag.px;
      if (Math.abs(dx) > 2) drag.moved = true;
      cueEditor.start = drag.start - (dx / geom.width) * geom.span;
      drawCueEditor();
      return;
    }
    const geom = cueGeometry();
    const raw = geom.ms(event.clientX - geom.rect.left);
    drag.ms = Math.min(Math.max(0, cueSnap(raw, event.shiftKey)), cueAxisMs());
    drag.moved = drag.moved || Math.abs(drag.ms - drag.was) > 0;
    $('#cueReadout').textContent = `${drag.cue.slot} · ${fmtClock(drag.ms / 1000)}`
      + ` (was ${fmtClock(drag.was / 1000)})`
      + (event.shiftKey ? ' · free' : '');
    drawCueEditor();
  });

  const endDrag = async (event) => {
    const drag = cueEditor.drag;
    if (!drag) return;
    cueEditor.drag = null;
    canvas.classList.remove('moving', 'grabbing');
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    if (drag.mode === 'pan') {
      // A click without a drag is a seek: the same gesture, told apart by travel.
      if (!drag.moved) seekFromCueEditor(drag.at);
      return;
    }
    if (!drag.moved) {
      // A flag that was clicked rather than dragged is a lock switch.
      if (drag.mode === 'flag') await toggleCueLock(drag.cue);
      else seekFromCueEditor(drag.at);
      return;
    }
    await moveCue(drag.cue, drag.ms);
  };
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', endDrag);
  const refit = () => {
    if ($('#cuePanel').hidden) return;
    sizeCueCanvas();
    drawCueEditor();
  };
  window.addEventListener('resize', refit);
  // The panel can change width without the window changing size -- a scrollbar
  // appearing, a long title wrapping -- and the canvas then has to follow its own
  // box, or the pointer stops landing on the peak it looks like it is on.
  if (window.ResizeObserver) new ResizeObserver(refit).observe(canvas);
  canvas.addEventListener('pointerleave', (event) => { if (!cueEditor.drag) $('#cueCanvas').style.cursor = 'crosshair'; });

  canvas.addEventListener('wheel', (event) => {
    if (!cueEditor.track) return;
    event.preventDefault();
    const geom = cueGeometry();
    const rect = geom.rect;
    const anchor = geom.ms(event.clientX - rect.left);
    const index = CUE_ZOOMS.indexOf(cueEditor.zoom);
    const next = Math.min(CUE_ZOOMS.length - 1, Math.max(0, (index < 0 ? 0 : index) + (event.deltaY < 0 ? 1 : -1)));
    if (CUE_ZOOMS[next] === cueEditor.zoom) return;
    cueEditor.zoom = CUE_ZOOMS[next];
    $('#cueZoom').value = String(cueEditor.zoom);
    centreCueView(anchor, (event.clientX - rect.left) / geom.width);
    drawCueEditor();
  }, { passive: false });
}

function hoverCueCursor(event) {
  if (!cueEditor.track || !cueEditor.plan) return;
  const hit = cueAt(cueGeometry(), event);
  $('#cueCanvas').style.cursor = hit ? 'ew-resize' : 'crosshair';
}

/* Put ``ms`` under the pointer (``fraction`` across the canvas), clamped to the track. */
function centreCueView(ms, fraction = 0.5) {
  const axis = cueAxisMs();
  const span = axis / cueEditor.zoom;
  cueEditor.start = ms - span * fraction;
  cueEditor.start = Math.min(Math.max(0, cueEditor.start), Math.max(0, axis - span));
}

function seekFromCueEditor(ms) {
  const track = cueEditor.track;
  if (!track || !canPreview(track)) return;
  if (!player.track || player.track.key !== track.key) {
    startPreview(track);
    player.seekOnLoad = ms / 1000;
    return;
  }
  seekTo(ms / 1000);
}

/*
 * The set row's badge counts the locked cues, and the plan is what knows them:
 * the track view is only refetched on a re-read of the library, and a badge that
 * disagreed with the panel next to it would be worse than no badge.
 */
function syncCueBadgeFromPlan() {
  const track = cueEditor.track;
  const plan = cueEditor.plan;
  if (!track || !plan) return;
  const locked = (plan.hot_cues || []).filter((cue) => cue.locked).length;
  const view = state.trackIndex.get(track.key) || track;
  if (view.cues) view.cues.locked = locked;
}

async function moveCue(cue, ms) {
  const track = cueEditor.track;
  const was = cue.ms;
  if (!track) return;
  cue.ms = ms;   // the drag already showed this; keep it if the write succeeds
  cue.seconds = ms / 1000;
  cue.locked = true;
  cue.edited = true;
  drawCueEditor();
  try {
    const answer = await api('/api/cues/edit', { key: track.key, slot: cue.slot, ms });
    if (cueEditor.plan && answer.plan) cueEditor.plan = answer.plan;
    syncCueBadgeFromPlan();
    $('#cueReadout').textContent = `${cue.slot} · ${fmtClock(ms / 1000)} · locked`;
    toast(`${cue.slot} moved to ${fmtClock(ms / 1000)} — locked, so Analyse cues leaves it here.`);
    drawCueEditor();
    renderSet();
  } catch (error) {
    cue.ms = was;
    cue.seconds = was / 1000;
    cue.locked = false;
    drawCueEditor();
    toast(error.message, true);
  }
}

async function toggleCueLock(cue) {
  const track = cueEditor.track;
  if (!track) return;
  const wanted = !cue.locked;
  try {
    const answer = await api('/api/cues/edit', { key: track.key, slot: cue.slot, locked: wanted });
    if (cueEditor.plan && answer.plan) cueEditor.plan = answer.plan;
    const now = (cueEditor.plan ? cueEditor.plan.hot_cues : []).find((item) => item.slot === cue.slot);
    if (now) { cue.locked = now.locked; cue.edited = now.edited; }
    syncCueBadgeFromPlan();
    toast(wanted
      ? `${cue.slot} locked at ${fmtClock(cue.ms / 1000)} — Analyse cues will keep it there.`
      : `${cue.slot} unlocked — the next Analyse cues may move it.`);
    drawCueEditor();
    renderSet();
  } catch (error) {
    toast(error.message, true);
  }
}

async function init() {
  // The app window runs its page under a transparent title bar (see
  // tools/macos/main.swift), so the header has to be told how far to move over
  // for the traffic lights.  In a browser there is nothing to clear.
  if (isAppShell()) document.documentElement.classList.add('shell-app');
  bindPlayer();
  renderQueue();
  attachEvents();
  bindCueEditor();
  syncPickButtons();
  try {
    await loadState();
  } catch (error) {
    toast(error.message, true);
  }
  renderPool();
  renderSet();
}

init();

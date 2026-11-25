/* ============================================================
   Your profile: photo, name, birthday, bio and what Lumi should know.
   Backend: modules/user_profile.py (/api/profile*).

     init({ api, state })      loads the profile and paints the sidebar chip
     openDialog()              the editor in a dialog (the chip opens it)
     buildEditor()             the same editor for Settings -> Profile
     paintAvatar(el, name)     a photo (or initial) in any avatar circle

   Photos are shrunk to 512 px in the browser before upload; the server
   re-encodes them to a 256 px PNG, which drops EXIF (GPS, camera) data.
   Everything from the server is written with textContent.
   ============================================================ */

let C = null;
let me = { profile: null, avatar_url: null, display_name: '', username: '' };
const listeners = new Set();
const MAX_SIDE = 512;

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

let started;
const startedP = new Promise((r) => { started = r; });

export function init(ctx) {
  C = ctx;
  started();
  const chip = document.getElementById('user-me');
  if (chip) chip.addEventListener('click', () => openDialog());
  load();
}

export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function current() { return me; }

let loading = null;
function load() {
  loading = (async () => {
    try {
      set(await C.api('/api/profile'));
    } catch { /* signed out, or the route isn't there: keep the initial */ }
  })();
  return loading;
}
/* Settings -> Profile waits for this, so the editor never opens half-empty. */
export async function ready() {
  await startedP;                     // a reload straight onto #/settings can get here before init()
  return loading || load();
}

function set(data) {
  me = { ...me, ...data };
  if (C.state.user && data.display_name) C.state.user.display_name = data.display_name;
  paintChip();
  for (const fn of listeners) { try { fn(me); } catch { /* one listener can't break the rest */ } }
}

export function paintAvatar(node, name) {
  node.replaceChildren();
  node.classList.toggle('has-photo', !!me.avatar_url);
  if (me.avatar_url) {
    const img = el('img');
    img.src = me.avatar_url;
    img.alt = '';
    img.onerror = () => { img.remove(); node.classList.remove('has-photo'); node.textContent = initial(name); };
    node.append(img);
  } else {
    node.textContent = initial(name);
  }
}
const initial = (name) => String(name || me.display_name || '?').trim().slice(0, 1).toUpperCase() || '?';

function paintChip() {
  const name = me.display_name || C.state.user?.display_name || C.state.user?.username || '—';
  const n = document.getElementById('user-name');
  if (n) n.textContent = name;
  const a = document.getElementById('user-avatar');
  if (a) paintAvatar(a, name);
}

/* ---------- photo ---------- */

function shrink(file) {
  return new Promise((resolve, reject) => {
    if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) { reject(new Error('Use a PNG, JPEG, WebP or GIF photo.')); return; }
    if (file.size > 25 * 1024 * 1024) { reject(new Error('That photo is over 25 MB.')); return; }
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
      const w = Math.max(1, Math.round(img.naturalWidth * scale));
      const h = Math.max(1, Math.round(img.naturalHeight * scale));
      const canvas = Object.assign(document.createElement('canvas'), { width: w, height: h });
      canvas.getContext('2d').drawImage(img, 0, 0, w, h);
      URL.revokeObjectURL(url);
      resolve(canvas.toDataURL(file.type === 'image/png' ? 'image/png' : 'image/jpeg', 0.9));
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("That file isn't a photo the browser can read.")); };
    img.src = url;
  });
}

/* ---------- editor ---------- */

const MONTHS = Array.from({ length: 12 }, (_, i) => new Date(2000, i, 1).toLocaleDateString([], { month: 'long' }));
const STATUS = [['', 'Choose…'], ['student', 'Student'], ['working', 'Working'], ['student_working', 'Student and working'],
  ['self_employed', 'Self-employed'], ['between', 'Between jobs'], ['retired', 'Retired']];
const TONES = [['playful', 'Playful'], ['warm', 'Warm'], ['balanced', 'Balanced'], ['direct', 'Direct']];
const LENGTHS = [['short', 'Short'], ['medium', 'Medium'], ['detailed', 'Detailed']];
const INTEREST_PRESETS = ['Gaming', 'Anime', 'Music', 'Coding', 'Art', 'Movies & TV', 'Reading', 'Sports',
  'Fitness', 'Cooking', 'Travel', 'Tech', 'Science', 'Photography', 'Fashion', 'VTubers'];

const FIELDS = [
  { key: 'display_name', label: 'Display name', hint: 'Shown in the app.', max: 40, me: true },
  { key: 'preferred_name', label: 'What should Lumi call you?', hint: 'Leave blank to use your display name.', max: 40 },
  { key: 'birthday', label: 'Birthday', type: 'birthday', wide: true },
  { key: 'location', label: 'Where you live', type: 'location', wide: true },
  { key: 'status', label: 'Work or school', type: 'select', options: STATUS },
  { key: 'occupation', label: 'School or workplace', hint: 'Optional, e.g. KSU.', max: 80 },
  { key: 'interests', label: 'Interests', type: 'interests', wide: true },
  { key: 'tone', label: 'How Lumi talks to you', type: 'buttons', options: TONES, wide: true },
  { key: 'reply_length', label: 'Reply length', type: 'buttons', options: LENGTHS, wide: true },
  { key: 'bio', label: 'Bio', hint: 'A few lines about you.', max: 500, area: 3 },
  { key: 'goals', label: 'What you’re working toward', max: 300, area: 2 },
  { key: 'likes', label: 'Things you like', max: 300, area: 2 },
  { key: 'dislikes', label: 'Things you don’t like', max: 300, area: 2 },
  { key: 'notes_for_lumi', label: 'Anything else Lumi should know', hint: 'Things to remember, things to avoid.', max: 500, area: 3 },
];

function select(id, options, value, label) {
  const s = el('select', 'settings-input pf-select');
  if (id) s.id = id;
  if (label) s.setAttribute('aria-label', label);
  for (const [v, t] of options) {
    const o = el('option', null, t);
    o.value = v;
    s.append(o);
  }
  s.value = value ?? '';
  return s;
}

/* Location: the browser's own permission prompt, then the city name. The
   point is rounded to about 1 km before it leaves the PC (BigDataCloud names
   the city). Then Lumi's weather city, units and timezone follow it. */
async function detectLocation() {
  if (!navigator.geolocation) throw new Error('This browser can’t share a location.');
  const pos = await new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(resolve, (e) =>
    reject(new Error(e.code === 1 ? 'Location permission was declined. Allow it for this site, then try again.'
                                  : 'Couldn’t get a location fix. Try again in a moment.')),
    { enableHighAccuracy: false, timeout: 12000, maximumAge: 3600000 }));
  const lat = Math.round(pos.coords.latitude * 100) / 100;
  const lon = Math.round(pos.coords.longitude * 100) / 100;
  let city = '', region = '', country = '';
  try {
    const r = await fetch(`https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=${lat}&longitude=${lon}&localityLanguage=en`,
      { credentials: 'omit', referrerPolicy: 'no-referrer' });
    const j = await r.json();
    city = String(j.city || j.locality || '').slice(0, 60);
    region = String(j.principalSubdivision || '').slice(0, 60);
    country = String(j.countryCode || '').slice(0, 4).toUpperCase();
  } catch { /* the point alone still works for the weather */ }
  const label = [city, region].filter(Boolean).join(', ') || `${lat.toFixed(2)}, ${lon.toFixed(2)}`;
  return { lat, lon, label, country, tz: Intl.DateTimeFormat().resolvedOptions().timeZone || '' };
}

async function applyLocation(loc) {
  const done = [];
  try {
    await C.api('/api/settings/display', { method: 'PUT', csrf: true, body: { values: {
      coords: { lat: loc.lat, lon: loc.lon, label: loc.label },
      ...(loc.country ? { units: ['US', 'LR', 'MM'].includes(loc.country) ? 'imperial' : 'metric' } : {}),
    } } });
    done.push('weather');
  } catch { /* weather is optional */ }
  if (loc.tz) {
    try {
      const cur = await C.api('/api/settings/general');
      if (cur.timezone !== loc.tz) {
        await C.api('/api/settings/general', { method: 'PUT', csrf: true, body: { values: { timezone: loc.tz } } });
        done.push('timezone');
      }
    } catch { /* keep the old timezone */ }
  }
  return done;
}

function buildFields(p) {
  const grid = el('div', 'pf-grid');
  const inputs = {};
  let tags = [...(p.interests || [])];
  let autoDetect = null;
  for (const f of FIELDS) {
    const row = el('div', 'pf-field' + (f.area || f.wide ? ' wide' : ''));
    const id = `pf-${f.key}`;
    const lbl = el(f.type === 'buttons' || f.type === 'interests' || f.type === 'birthday' ? 'div' : 'label', 'pf-label', f.label);
    if (lbl.tagName === 'LABEL') lbl.htmlFor = id; else lbl.id = `${id}-label`;
    row.append(lbl);

    if (f.type === 'birthday') {
      const v = p.birthday || '';
      const [y, m, d] = v.startsWith('--') ? ['', v.slice(2, 4), v.slice(5, 7)] : v ? v.split('-') : ['', '', ''];
      const year = new Date().getFullYear();
      const months = select(`${id}-m`, [['', 'Month'], ...MONTHS.map((n, i) => [String(i + 1).padStart(2, '0'), n])], m, 'Birth month');
      const days = select(`${id}-d`, [['', 'Day'], ...Array.from({ length: 31 }, (_, i) => [String(i + 1).padStart(2, '0'), String(i + 1)])], d, 'Birth day');
      const years = select(`${id}-y`, [['', 'Year (optional)'], ...Array.from({ length: year - 1919 }, (_, i) => [String(year - i), String(year - i)])], y, 'Birth year');
      const wrap = el('div', 'pf-bday');
      wrap.setAttribute('role', 'group');
      wrap.setAttribute('aria-labelledby', `${id}-label`);
      wrap.append(months, days, years);
      row.append(wrap, el('div', 'pf-hint', 'Lumi will know when it’s your birthday. The year is optional.'));
      inputs.birthday = { get: () => (!months.value || !days.value ? '' : years.value ? `${years.value}-${months.value}-${days.value}` : `--${months.value}-${days.value}`) };
    } else if (f.type === 'location') {
      const wrap = el('div', 'pf-loc');
      const input = Object.assign(el('input', 'settings-input'), { type: 'text', id, maxLength: 80, value: p.location || '', placeholder: 'Detecting…' });
      const detect = el('button', 'btn btn-sm', 'Detect');
      detect.type = 'button';
      detect.title = 'Use this device’s location';
      const msg = el('div', 'pf-hint');
      msg.setAttribute('role', 'status');
      const defaultHint = 'Detected from this device. Lumi’s weather and timezone follow it. You can also type a city.';
      msg.textContent = defaultHint;
      const run = async (quiet) => {
        detect.disabled = true; msg.className = 'pf-hint'; msg.textContent = 'Finding you…';
        try {
          const loc = await detectLocation();
          input.value = loc.label;
          const done = await applyLocation(loc);
          msg.textContent = `Detected ${loc.label}` + (done.length ? ` · ${done.join(' and ')} updated` : '') + '. Save to keep it in your profile.';
        } catch (e) {
          msg.className = quiet ? 'pf-hint' : 'pf-hint bad';
          msg.textContent = quiet ? defaultHint : e.message;
          if (!input.value) input.placeholder = 'e.g. Kennesaw, GA';
        } finally { detect.disabled = false; }
      };
      detect.addEventListener('click', () => run(false));
      if (!p.location) autoDetect = () => run(true);         // nothing saved yet: detect on open
      else input.placeholder = 'e.g. Kennesaw, GA';
      wrap.append(input, detect);
      row.append(wrap, msg);
      inputs.location = { get: () => input.value };
    } else if (f.type === 'select') {
      const s = select(id, f.options, p[f.key] || '');
      row.append(s);
      inputs[f.key] = { get: () => s.value };
    } else if (f.type === 'buttons') {
      const group = el('div', 'pf-seg');
      group.setAttribute('role', 'radiogroup');
      group.setAttribute('aria-labelledby', `${id}-label`);
      let value = p[f.key] || '';
      const paint = () => group.querySelectorAll('button').forEach((b) => {
        const on = b.dataset.v === value;
        b.classList.toggle('on', on); b.setAttribute('aria-checked', String(on));
      });
      for (const [v, t] of f.options) {
        const b = el('button', 'pf-seg-btn', t);
        b.type = 'button'; b.dataset.v = v; b.setAttribute('role', 'radio');
        b.addEventListener('click', () => { value = value === v ? '' : v; paint(); });   // click again to clear
        group.append(b);
      }
      paint();
      row.append(group);
      inputs[f.key] = { get: () => value };
    } else if (f.type === 'interests') {
      const chips = el('div', 'pf-chips');
      chips.setAttribute('aria-labelledby', `${id}-label`);
      const custom = Object.assign(el('input', 'settings-input pf-custom'), { id, maxLength: 40, placeholder: 'Add your own and press Enter' });
      const has = (t) => tags.some((y) => y.toLowerCase() === t.toLowerCase());
      const draw = () => {
        chips.replaceChildren();
        const extra = tags.filter((t) => !INTEREST_PRESETS.some((pz) => pz.toLowerCase() === t.toLowerCase()));
        for (const t of [...INTEREST_PRESETS, ...extra]) {
          const on = has(t);
          const b = el('button', 'pf-chip' + (on ? ' on' : ''), t);
          b.type = 'button';
          b.setAttribute('aria-pressed', String(on));
          b.addEventListener('click', () => {
            tags = on ? tags.filter((y) => y.toLowerCase() !== t.toLowerCase()) : (tags.length < 20 ? [...tags, t] : tags);
            draw();
          });
          chips.append(b);
        }
      };
      const add = () => {
        for (const t of custom.value.split(',').map((s) => s.trim().slice(0, 40)).filter(Boolean)) {
          if (tags.length < 20 && !has(t)) tags.push(t);
        }
        custom.value = ''; draw();
      };
      custom.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add(); } });
      custom.addEventListener('blur', add);
      draw();
      row.append(chips, custom);
      inputs.interests = { get: () => { add(); return tags; } };
    } else {
      const input = f.area ? el('textarea', 'settings-input pf-area') : el('input', 'settings-input');
      if (f.area) input.rows = f.area; else input.type = 'text';
      input.id = id;
      input.maxLength = f.max;
      input.value = f.me ? (me.display_name || C.state.user?.display_name || '') : (p[f.key] || '');
      row.append(input);
      const foot = el('div', 'pf-foot');
      if (f.hint) foot.append(el('div', 'pf-hint', f.hint));
      if (f.area) {
        const count = el('div', 'pf-count');
        const upd = () => { count.textContent = `${input.value.length}/${f.max}`; };
        input.addEventListener('input', upd); upd();
        foot.append(count);
      }
      row.append(foot);
      inputs[f.key] = { get: () => input.value };
    }
    grid.append(row);
  }
  return { grid, inputs, autoDetect };
}

export function buildEditor({ onDone } = {}) {
  const form = el('form', 'pf');
  form.noValidate = true;
  const p = { ...(me.profile || {}) };

  // Photo
  const photo = el('div', 'pf-photo');
  const big = el('div', 'pf-avatar');
  big.setAttribute('aria-hidden', 'true');
  const paintBig = () => paintAvatar(big, me.display_name || C.state.user?.display_name);
  paintBig();
  const file = Object.assign(el('input'), { type: 'file', accept: 'image/png,image/jpeg,image/webp,image/gif', hidden: true });
  const upload = el('button', 'btn btn-sm', 'Upload photo');
  upload.type = 'button';
  const remove = el('button', 'btn btn-sm btn-ghost', 'Remove');
  remove.type = 'button';
  remove.hidden = !me.avatar_url;
  const photoMsg = el('div', 'pf-msg');
  photoMsg.setAttribute('role', 'status');
  const photoText = el('div', 'pf-photo-text');
  photoText.append(el('div', 'pf-photo-title', 'Profile photo'),
    el('div', 'pf-hint', 'PNG, JPEG, WebP or GIF. Stored on this PC; location data in the photo is removed.'));
  const photoBtns = el('div', 'pf-photo-btns');
  photoBtns.append(upload, remove);
  photoText.append(photoBtns, photoMsg);
  photo.append(big, photoText, file);

  const sendPhoto = async (f) => {
    if (!f) return;
    photoMsg.className = 'pf-msg'; photoMsg.textContent = 'Uploading…';
    try {
      const image = await shrink(f);
      set(await C.api('/api/profile/avatar', { method: 'POST', csrf: true, body: { image } }));
      paintBig(); remove.hidden = false;
      photoMsg.textContent = 'Photo updated.';
    } catch (e) { photoMsg.className = 'pf-msg bad'; photoMsg.textContent = e.message; }
  };
  upload.addEventListener('click', () => file.click());
  file.addEventListener('change', () => { sendPhoto(file.files[0]); file.value = ''; });
  big.addEventListener('dragover', (e) => { e.preventDefault(); big.classList.add('drop'); });
  big.addEventListener('dragleave', () => big.classList.remove('drop'));
  big.addEventListener('drop', (e) => { e.preventDefault(); big.classList.remove('drop'); sendPhoto(e.dataTransfer.files[0]); });
  remove.addEventListener('click', async () => {
    try {
      set(await C.api('/api/profile/avatar/delete', { method: 'POST', csrf: true, body: {} }));
      paintBig(); remove.hidden = true; photoMsg.textContent = 'Photo removed.';
    } catch (e) { photoMsg.className = 'pf-msg bad'; photoMsg.textContent = e.message; }
  });
  form.append(photo);

  // Fields
  const { grid, inputs, autoDetect } = buildFields(p);
  form.append(grid);

  // Share switch + actions
  const share = el('label', 'pf-share');
  const sw = el('span', 'switch');
  const shareIn = Object.assign(el('input'), { type: 'checkbox', checked: p.share_with_lumi !== false });
  sw.append(shareIn, el('span', 'switch-slider'));
  const shareText = el('div');
  shareText.append(el('div', 'pf-share-title', 'Let Lumi use my profile'),
    el('div', 'pf-hint', 'She reads it each turn to know you better. It stays on this PC unless your chat model is a cloud provider.'));
  share.append(shareText, sw);
  form.append(share);

  const actions = el('div', 'pf-actions');
  const msg = el('div', 'pf-msg');
  msg.setAttribute('role', 'status');
  const save = el('button', 'btn btn-primary', 'Save profile');
  save.type = 'submit';
  actions.append(msg, save);
  if (onDone) {
    const cancel = el('button', 'btn btn-ghost', 'Close');
    cancel.type = 'button';
    cancel.addEventListener('click', onDone);
    actions.insertBefore(cancel, save);
  }
  form.append(actions);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = { share_with_lumi: shareIn.checked };
    for (const [k, v] of Object.entries(inputs)) body[k] = v.get();
    save.disabled = true; msg.className = 'pf-msg'; msg.textContent = 'Saving…';
    try {
      set(await C.api('/api/profile', { method: 'POST', csrf: true, body }));
      msg.textContent = 'Saved. Lumi will use it from her next reply.';
      if (onDone) setTimeout(onDone, 700);
    } catch (err) {
      msg.className = 'pf-msg bad'; msg.textContent = err.message;
    } finally { save.disabled = false; }
  });
  if (autoDetect) setTimeout(() => { if (form.isConnected) autoDetect(); }, 300);
  return form;
}

/* ---------- dialog ---------- */

export async function openDialog() {
  if (document.querySelector('.pf-modal')) return;
  await load();
  const back = el('div', 'pf-modal');
  const box = el('div', 'pf-dialog');
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  box.setAttribute('aria-labelledby', 'pf-title');
  const head = el('div', 'pf-head');
  const title = el('h2', 'pf-title', 'Your profile');
  title.id = 'pf-title';
  const x = el('button', 'btn btn-ghost btn-sm', '✕');
  x.type = 'button';
  x.setAttribute('aria-label', 'Close');
  head.append(title, x);
  const opener = document.activeElement;
  const close = () => { back.remove(); document.removeEventListener('keydown', onKey); if (opener && opener.focus) opener.focus(); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  x.addEventListener('click', close);
  back.addEventListener('mousedown', (e) => { if (e.target === back) close(); });
  document.addEventListener('keydown', onKey);
  box.append(head, el('p', 'pf-intro', 'Tell Lumi about yourself so she can talk to you as you. Everything here is optional.'),
    buildEditor({ onDone: close }));
  back.append(box);
  document.body.append(back);
  const first = box.querySelector('#pf-display_name');
  if (first) first.focus();
}

/* ============================================================
   Display preferences: 12/24-hour clock, temperature units and the
   weather city (Settings → General → Display). Stored on the server
   (data/display.json) so Lumi Island follows the same choice; every
   change applies immediately, no Save button.
   Exports `prefs` and `timeOpts()` for other views (the Workspace tab
   uses them so its times match the island).
   ============================================================ */

let C = null;            // { api }
export function init(ctx) { C = ctx; }

export const prefs = { time_format: '12h', units: 'imperial', location: '', place: '', has_location: false };

export const is12h = () => prefs.time_format !== '24h';
/** Options for toLocaleTimeString that honour the 12/24-hour setting. */
export function timeOpts() {
  return is12h() ? { hour: 'numeric', minute: '2-digit', hour12: true }
                 : { hour: '2-digit', minute: '2-digit', hour12: false, hourCycle: 'h23' };
}

export async function load() {
  try { Object.assign(prefs, await C.api('/api/settings/display')); } catch (_) { /* keep defaults */ }
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

async function save(values) {
  const res = await C.api('/api/settings/display', { method: 'PUT', body: { values }, csrf: true });
  Object.assign(prefs, res.prefs);
  return res;
}

function chips(options, current, onPick) {
  const row = el('div', 'disp-chips');
  for (const [value, label] of options) {
    const b = el('button', 'filter-chip' + (value === current ? ' active' : ''), label);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(value === current));
    b.addEventListener('click', async () => {
      if (b.classList.contains('active')) return;
      row.querySelectorAll('.filter-chip').forEach((x) => { x.classList.toggle('active', x === b); x.setAttribute('aria-pressed', String(x === b)); });
      try { await onPick(value); } catch (_) { /* the caller shows the error */ }
    });
    row.append(b);
  }
  return row;
}

/** The Display card for Settings → General. */
export function buildDisplayCard() {
  const wrap = el('div', 'settings-field disp');
  wrap.append(el('div', 'settings-label', 'Display'));
  const status = el('div', 'disp-status');
  status.setAttribute('role', 'status');
  const say = (text, bad) => { status.textContent = text; status.classList.toggle('bad', !!bad); };

  const preview = el('span', 'disp-preview');
  const tick = () => { preview.textContent = new Date().toLocaleTimeString([], timeOpts()); };
  tick();

  // time format
  const timeRow = el('div', 'disp-row');
  timeRow.append(el('div', 'disp-name', 'Time format'),
    chips([['12h', '12-hour'], ['24h', '24-hour']], prefs.time_format, async (v) => {
      try { await save({ time_format: v }); tick(); say('Saved. Lumi Island and the Workspace tab follow this.'); }
      catch (e) { say(e.message, true); throw e; }
    }),
    preview);

  // units
  const unitRow = el('div', 'disp-row');
  unitRow.append(el('div', 'disp-name', 'Temperature'),
    chips([['imperial', '°F'], ['metric', '°C']], prefs.units, async (v) => {
      try {
        const res = await save({ units: v });
        say(res.weather ? `Saved. ${res.weather.place}: ${res.weather.temp}${res.weather.unit}, ${res.weather.text}.` : 'Saved.');
      } catch (e) { say(e.message, true); throw e; }
    }));

  // weather city
  const cityRow = el('div', 'disp-row disp-city');
  cityRow.append(el('div', 'disp-name', 'Weather city'));
  const input = el('input', 'settings-input');
  input.maxLength = 80;
  input.placeholder = 'e.g. Kennesaw';
  input.value = prefs.location || '';
  input.setAttribute('aria-label', 'Weather city');
  const apply = el('button', 'btn btn-sm btn-primary', 'Save');
  apply.type = 'button';
  const clear = el('button', 'btn btn-sm', 'Clear');
  clear.type = 'button';
  const go = async (value) => {
    apply.disabled = clear.disabled = true;
    say(value ? 'Looking it up…' : 'Clearing…');
    try {
      const res = await save({ location: value });
      input.value = res.prefs.location;
      if (!value) say('Cleared. No weather will be requested.');
      else if (res.weather) say(`${res.weather.place} · ${res.weather.temp}${res.weather.unit}, ${res.weather.text}`);
      else say(`Saved ${res.prefs.place}. ${res.weather_error || ''}`.trim());
    } catch (e) { say(e.message, true); }
    apply.disabled = clear.disabled = false;
  };
  apply.addEventListener('click', () => go(input.value.trim()));
  clear.addEventListener('click', () => { input.value = ''; go(''); });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); go(input.value.trim()); } });
  cityRow.append(input, apply, clear);

  wrap.append(timeRow, unitRow, cityRow, status,
    el('div', 'settings-field-help',
      'Shown on Lumi Island’s Home tab. Weather comes from Open-Meteo (free, no account). Only the city name you type, ' +
      'then its coordinates, are sent; nothing is requested until you enter a city.'));
  say(prefs.has_location ? `Weather for ${prefs.place}.` : '');
  load().then(() => {                       // reflect what's saved, in case the page loaded before it
    input.value = prefs.location || '';
    timeRow.querySelectorAll('.filter-chip').forEach((b, i) => b.classList.toggle('active', (i === 0) === is12h()));
    unitRow.querySelectorAll('.filter-chip').forEach((b, i) => b.classList.toggle('active', (i === 0) === (prefs.units === 'imperial')));
    tick();
    say(prefs.has_location ? `Weather for ${prefs.place}.` : '');
  });
  return wrap;
}

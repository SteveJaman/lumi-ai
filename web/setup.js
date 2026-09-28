/* ============================================================
   Multi-step setup wizard (Phase 10.8).

   Steps:
     account       — first-run register form
     providers     — optional cloud LLM provider setup
     integrations  — optional Discord / Fish Audio / VTS setup
     done          — summary + enter Lumi

   Server-side gating:
     - No users      → always start at account.
     - Authed users  → honour ?step=..., default to providers.
     - Unauthed + users exist → redirect to /login.
   ============================================================ */

const $ = s => document.querySelector(s);

const state = {
  step: 'account',
  providers: [],
  integrations: [],
  csrf: null,
};

/* ---- Utilities ----------------------------------------------- */
async function api(path, { method = 'GET', body, csrf = false } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (csrf && state.csrf) headers['X-CSRF-Token'] = state.csrf;
  const res = await fetch(path, {
    method, headers, credentials: 'same-origin',
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    const msg = (data && data.error) || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function showError(msg) {
  const el = $('#auth-error');
  el.textContent = msg;
  el.classList.add('visible');
}

function hideError() {
  $('#auth-error').classList.remove('visible');
}

function setProgress(step) {
  const dots = document.querySelectorAll('.setup-step-dot');
  const order = ['account', 'providers', 'integrations', 'done'];
  const activeIdx = order.indexOf(step);
  dots.forEach((d, i) => {
    d.classList.toggle('active', i === activeIdx);
    d.classList.toggle('complete', i < activeIdx);
  });
}

/* ---- Boot ---------------------------------------------------- */
(async () => {
  let setupStatus = { needs_setup: true };
  try {
    const r = await fetch('/setup-status', { credentials: 'same-origin' });
    setupStatus = await r.json();
  } catch {}

  if (setupStatus.needs_setup) {
    state.step = 'account';
    return renderAccount();
  }

  // Users exist. Are we authed?
  try {
    const me = await api('/auth/me');
    state.csrf = me.csrf;
  } catch {
    location.replace('/login');
    return;
  }

  // Authed. Read ?step= or default to providers.
  const params = new URLSearchParams(location.search);
  const step = (params.get('step') || 'providers').toLowerCase();
  if (!['providers', 'integrations', 'done'].includes(step)) {
    location.replace('/setup?step=providers');
    return;
  }

  if (step === 'providers')    return renderProviders();
  if (step === 'integrations') return renderIntegrations();
  if (step === 'done')         return renderDone();
})();

/* ---- Step 1: account ----------------------------------------- */
function renderAccount() {
  state.step = 'account';
  setProgress('account');
  $('#setup-body').innerHTML = `
    <div class="setup-header">
      <div class="auth-logo">◆</div>
      <div class="setup-title">Welcome to Lumi</div>
      <div class="setup-sub">Create the first account on this machine.</div>
    </div>
    <form id="setup-form" class="setup-form">
      <div class="field">
        <label class="label">Username</label>
        <input class="input" id="setup-username" required
               autocomplete="username">
      </div>
      <div class="field">
        <label class="label">Display name</label>
        <input class="input" id="setup-display"
               placeholder="How should Lumi address you?"
               autocomplete="name">
      </div>
      <div class="field">
        <label class="label">Password (min 8 chars)</label>
        <input class="input" id="setup-password" type="password" required
               autocomplete="new-password">
      </div>
      <button class="btn btn-primary" type="submit"
              style="width:100%" id="setup-submit">Create account</button>
    </form>
    <div class="setup-foot">
      Everything stays on this machine. No cloud.
    </div>
  `;

  $('#setup-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    hideError();
    const submit = $('#setup-submit');
    submit.disabled = true;
    try {
      const res = await fetch('/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          username: $('#setup-username').value.trim(),
          password: $('#setup-password').value,
          display_name: $('#setup-display').value.trim() || undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Registration failed');
      location.href = '/setup?step=providers';
    } catch (err) {
      showError(err.message);
      submit.disabled = false;
    }
  });
}

/* ---- Step 2: providers --------------------------------------- */
async function renderProviders() {
  state.step = 'providers';
  setProgress('providers');
  $('#setup-body').innerHTML = `
    <div class="setup-header">
      <div class="setup-title">Connect a provider</div>
      <div class="setup-sub">
        Optional. Point Lumi at OpenAI, Claude, Gemini, Groq, or
        OpenRouter — or skip and stay entirely local on Ollama.
      </div>
    </div>
    <div class="setup-grid" id="setup-providers"></div>
    <div class="setup-actions">
      <button class="btn" id="setup-providers-skip">Skip — stay on Ollama</button>
      <button class="btn btn-primary" id="setup-providers-next">Continue</button>
    </div>
  `;

  let providers = [];
  try {
    const res = await api('/api/providers');
    providers = (res.providers || []).filter(p => p.id !== 'ollama');
  } catch {}

  const grid = $('#setup-providers');
  grid.innerHTML = '';
  for (const p of providers) grid.appendChild(buildProviderCard(p));

  $('#setup-providers-skip').addEventListener('click', () => {
    location.href = '/setup?step=integrations';
  });
  $('#setup-providers-next').addEventListener('click', () => {
    location.href = '/setup?step=integrations';
  });
}

function buildProviderCard(p) {
  const card = document.createElement('div');
  card.className = 'setup-provider-card';
  card.innerHTML = `
    <div class="setup-provider-name">${escapeHtml(p.label)}</div>
    <div class="setup-provider-status">
      ${p.configured ? '✓ configured' : 'not configured'}
    </div>
    <button class="btn btn-sm" data-id="${escapeHtml(p.id)}">
      ${p.configured ? 'Edit key' : 'Add API key'}
    </button>
  `;
  card.querySelector('button').addEventListener('click', () => openProviderModal(p));
  return card;
}

function openProviderModal(p) {
  const existing = document.querySelector('.modal-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal-card">
      <div class="modal-header">
        <div class="modal-title">${escapeHtml(p.label)}</div>
        <button class="modal-close">×</button>
      </div>
      <div class="field">
        <label class="label">API key</label>
        <input class="input" id="setup-provider-key" type="password"
               placeholder="${p.api_key_masked || 'sk-…'}">
      </div>
      <div class="provider-hint">
        Stored locally in .env. Never sent to any server except
        ${escapeHtml(p.label)}.
      </div>
      <div class="modal-actions">
        <button class="btn" id="setup-provider-cancel">Cancel</button>
        <button class="btn btn-primary" id="setup-provider-save">Save key</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  overlay.querySelector('.modal-close').addEventListener('click', () => overlay.remove());
  overlay.querySelector('#setup-provider-cancel').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', e => {
    if (e.target === overlay) overlay.remove();
  });

  overlay.querySelector('#setup-provider-save').addEventListener('click', async () => {
    const key = overlay.querySelector('#setup-provider-key').value.trim();
    if (!key) { overlay.remove(); return; }
    const btn = overlay.querySelector('#setup-provider-save');
    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
      await api(`/api/providers/${encodeURIComponent(p.id)}`, {
        method: 'PUT',
        body: { api_key: key },
        csrf: true,
      });
      overlay.remove();
      await renderProviders();
    } catch (e) {
      alert('Save failed: ' + e.message);
      btn.disabled = false;
      btn.textContent = 'Save key';
    }
  });

  setTimeout(() => overlay.querySelector('#setup-provider-key').focus(), 50);
}

/* ---- Step 3: integrations ------------------------------------ */
async function renderIntegrations() {
  state.step = 'integrations';
  setProgress('integrations');
  $('#setup-body').innerHTML = `
    <div class="setup-header">
      <div class="setup-title">Connect integrations</div>
      <div class="setup-sub">
        Optional. Discord, Fish Audio (voice), and VTube Studio
        (avatar). You can always configure these later from the
        Integrations page.
      </div>
    </div>
    <div class="setup-grid" id="setup-integrations"></div>
    <div class="setup-actions">
      <button class="btn" id="setup-integrations-skip">Skip for now</button>
      <button class="btn btn-primary" id="setup-integrations-next">Continue</button>
    </div>
  `;

  let integrations = [];
  try {
    const res = await api('/api/integrations');
    integrations = (res.integrations || []).filter(
      it => ['discord', 'fish_audio', 'vtube_studio'].includes(it.id)
    );
  } catch {}

  const grid = $('#setup-integrations');
  grid.innerHTML = '';
  for (const it of integrations) grid.appendChild(buildIntegrationCard(it));

  $('#setup-integrations-skip').addEventListener('click', () => {
    location.href = '/setup?step=done';
  });
  $('#setup-integrations-next').addEventListener('click', () => {
    location.href = '/setup?step=done';
  });
}

function buildIntegrationCard(it) {
  const card = document.createElement('div');
  card.className = 'setup-integration-card';
  const configured = (it.fields || []).some(f => f.is_set);
  card.innerHTML = `
    <div class="setup-integration-emoji">${it.emoji || '◆'}</div>
    <div class="setup-integration-name">${escapeHtml(it.label)}</div>
    <div class="setup-integration-status">
      ${configured ? '✓ configured' : 'not configured'}
    </div>
    <button class="btn btn-sm">${configured ? 'Edit' : 'Configure'}</button>
  `;
  card.querySelector('button').addEventListener('click', () => openIntegrationModal(it));
  return card;
}

function openIntegrationModal(it) {
  const existing = document.querySelector('.modal-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  const card = document.createElement('div');
  card.className = 'modal-card';

  card.innerHTML = `
    <div class="modal-header">
      <div class="modal-title">${escapeHtml(it.label)}</div>
      <button class="modal-close">×</button>
    </div>
    <div class="provider-hint" style="margin-bottom:16px">
      Leave a field blank to keep its current value.
    </div>
    <div id="setup-integration-fields"></div>
    <div class="modal-actions">
      <button class="btn" id="setup-integration-cancel">Cancel</button>
      <button class="btn btn-primary" id="setup-integration-save">Save</button>
    </div>
  `;

  const fields = card.querySelector('#setup-integration-fields');
  const inputs = {};
  for (const f of (it.fields || [])) {
    const field = document.createElement('div');
    field.className = 'field';
    field.innerHTML = `
      <label class="label">${escapeHtml(f.label)}</label>
      <input class="input" type="${f.secret ? 'password' : 'text'}"
             placeholder="${f.is_set ? escapeHtml(f.masked || 'set') : escapeHtml(f.placeholder || '')}">
    `;
    fields.appendChild(field);
    inputs[f.key] = field.querySelector('input');
  }

  overlay.appendChild(card);
  document.body.appendChild(overlay);

  overlay.querySelector('.modal-close').addEventListener('click', () => overlay.remove());
  overlay.querySelector('#setup-integration-cancel').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

  overlay.querySelector('#setup-integration-save').addEventListener('click', async () => {
    const values = {};
    for (const [key, inp] of Object.entries(inputs)) {
      const v = inp.value.trim();
      if (v) values[key] = v;
    }
    if (!Object.keys(values).length) { overlay.remove(); return; }
    const btn = overlay.querySelector('#setup-integration-save');
    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
      await api(`/api/integrations/${encodeURIComponent(it.id)}`, {
        method: 'PUT', body: { values }, csrf: true,
      });
      overlay.remove();
      await renderIntegrations();
    } catch (e) {
      alert('Save failed: ' + e.message);
      btn.disabled = false;
      btn.textContent = 'Save';
    }
  });
}

/* ---- Step 4: done -------------------------------------------- */
function renderDone() {
  state.step = 'done';
  setProgress('done');
  $('#setup-body').innerHTML = `
    <div class="setup-header">
      <div class="auth-logo">◆</div>
      <div class="setup-title">Setup complete</div>
      <div class="setup-sub">
        Lumi is ready. You can change any of these settings later
        from the Integrations and Models pages.
      </div>
    </div>
    <div class="setup-done-list">
      <div class="setup-done-item">✓ Local Ollama runtime</div>
      <div class="setup-done-item">✓ Account + session auth</div>
      <div class="setup-done-item">✓ Memory, self-model, heartbeat</div>
      <div class="setup-done-item">✓ Chat, dashboard, memory browser</div>
    </div>
    <div class="setup-actions setup-actions-center">
      <button class="btn btn-primary btn-lg" id="setup-enter">
        Enter Lumi →
      </button>
    </div>
  `;
  $('#setup-enter').addEventListener('click', () => {
    location.href = '/';
  });
}
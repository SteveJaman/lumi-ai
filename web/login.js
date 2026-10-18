/* ============================================================
   Standalone login / register page.
   Boots, checks whether the user is already authenticated, and
   redirects into the app. Otherwise renders the form and posts
   to /auth/login or /auth/register.
   ============================================================ */

const $ = s => document.querySelector(s);

let _mode = 'login';

function updateUI() {
  if (_mode === 'register') {
    $('#auth-title').textContent = 'Create account';
    $('#auth-sub').textContent = 'Local to this machine. No cloud.';
    $('#auth-submit').textContent = 'Register';
    $('#auth-switch-text').textContent = 'Already have an account?';
    $('#auth-switch-link').textContent = 'Sign in';
    $('#auth-display-field').style.display = '';
    $('#auth-password').autocomplete = 'new-password';
  } else {
    $('#auth-title').textContent = 'Welcome back';
    $('#auth-sub').textContent = 'Sign in to continue';
    $('#auth-submit').textContent = 'Sign in';
    $('#auth-switch-text').textContent = 'No account?';
    $('#auth-switch-link').textContent = 'Register';
    $('#auth-display-field').style.display = 'none';
    $('#auth-password').autocomplete = 'current-password';
  }
  hideError();
}

function showError(msg) {
  const el = $('#auth-error');
  el.textContent = msg;
  el.classList.add('visible');
}

function hideError() {
  $('#auth-error').classList.remove('visible');
}

$('#auth-switch-link').addEventListener('click', () => {
  _mode = _mode === 'login' ? 'register' : 'login';
  updateUI();
});

$('#auth-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  hideError();

  const submit   = $('#auth-submit');
  const username = $('#auth-username').value.trim();
  const password = $('#auth-password').value;
  const display  = $('#auth-display').value.trim();

  submit.disabled = true;
  try {
    const path = _mode === 'register' ? '/auth/register' : '/auth/login';
    const body = _mode === 'register'
      ? { username, password, display_name: display || undefined }
      : { username, password };

    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch {}
    if (!res.ok) {
      throw new Error((data && data.error) || `HTTP ${res.status}`);
    }
    location.href = '/';
  } catch (err) {
    showError(err.message);
    submit.disabled = false;
  }
});

/* ---- Boot ---------------------------------------------------- */
(async () => {
  // No users on this machine? Send to first-run setup.
  try {
    const r = await fetch('/setup-status', { credentials: 'same-origin' });
    const data = await r.json();
    if (data.needs_setup && data.remote) {
      // The first account can only be created on the host machine.
      updateUI();
      $('#auth-submit').disabled = true;
      showError('Lumi has not been set up yet. Create the first account ' +
                'on the host machine at http://127.0.0.1:5000.');
      return;
    }
    if (data.needs_setup) {
      location.href = '/setup';
      return;
    }
  } catch {}

  // Already signed in? Straight to the app.
  try {
    const me = await fetch('/auth/me', { credentials: 'same-origin' });
    if (me.ok) {
      location.href = '/';
      return;
    }
  } catch {}

  // Honor #register so /login#register lands in register mode.
  if (location.hash === '#register') _mode = 'register';

  updateUI();
})();
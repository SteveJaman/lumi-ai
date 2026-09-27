const $ = s => document.querySelector(s);

$('#setup-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const submit = $('#setup-submit');
  const err = $('#auth-error');
  err.classList.remove('visible');
  submit.disabled = true;
  try {
    const res = await fetch('/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: $('#setup-username').value.trim(),
        password: $('#setup-password').value,
        display_name: $('#setup-display').value.trim() || undefined,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Registration failed');
    location.href = '/';
  } catch (e) {
    err.textContent = e.message;
    err.classList.add('visible');
    submit.disabled = false;
  }
});
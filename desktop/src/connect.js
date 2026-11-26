// Lumi Desktop's connect screen: remembers servers, checks which are up,
// and loads the chosen one's web app into this window.
(() => {
  const KEY = 'lumi.desktop.servers';
  const LAST = 'lumi.desktop.last';
  const THIS_PC = { name: 'This PC', url: 'http://127.0.0.1:5000' };
  const $ = (id) => document.getElementById(id);
  const status = (text, bad = false) => {
    $('status').textContent = text;
    $('status').classList.toggle('bad', bad);
  };

  // Only an http(s) origin: Lumi's web app lives at the root.
  const clean = (raw) => {
    let u;
    try { u = new URL(String(raw).trim()); } catch { return null; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return u.origin;
  };
  const load = () => {
    try {
      const s = JSON.parse(localStorage.getItem(KEY) || '[]');
      return Array.isArray(s) ? s.filter((x) => x && clean(x.url)) : [];
    } catch { return []; }
  };
  const save = (list) => localStorage.setItem(KEY, JSON.stringify(list.slice(0, 8)));
  const all = () => [THIS_PC, ...load().filter((s) => s.url !== THIS_PC.url)];

  // Reachable? An opaque no-cors answer is enough: anything but a network error.
  async function probe(url, ms = 2500) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms);
    try {
      await fetch(`${url}/healthz`, { mode: 'no-cors', cache: 'no-store', signal: ctl.signal });
      return true;
    } catch {
      return false;
    } finally {
      clearTimeout(t);
    }
  }

  async function go(url) {
    status(`Connecting to ${url}…`);
    if (!(await probe(url, 5000))) {
      status(`Can't reach ${url}. Is Lumi running there?`, true);
      return;
    }
    localStorage.setItem(LAST, url);
    window.location.replace(`${url}/`);
  }

  function render() {
    const ul = $('servers');
    ul.textContent = '';
    for (const s of all()) {
      const li = document.createElement('li');
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'server';
      const dot = document.createElement('span');
      dot.className = 'dot';
      dot.setAttribute('aria-hidden', 'true');
      const txt = document.createElement('span');
      txt.className = 'txt';
      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = s.name;
      const addr = document.createElement('span');
      addr.className = 'addr';
      addr.textContent = s.url;
      txt.append(name, addr);
      b.append(dot, txt);
      b.addEventListener('click', () => go(s.url));
      if (s.url !== THIS_PC.url) {
        const f = document.createElement('span');
        f.className = 'forget';
        f.setAttribute('role', 'button');
        f.tabIndex = 0;
        f.textContent = 'Forget';
        f.setAttribute('aria-label', `Forget ${s.url}`);
        const forget = (e) => {
          e.stopPropagation();
          save(load().filter((x) => x.url !== s.url));
          if (localStorage.getItem(LAST) === s.url) localStorage.removeItem(LAST);
          render();
        };
        f.addEventListener('click', forget);
        f.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') forget(e); });
        b.appendChild(f);
      }
      li.appendChild(b);
      ul.appendChild(li);
      probe(s.url).then((up) => {
        dot.classList.add(up ? 'ok' : 'bad');
        b.title = up ? 'Lumi is up' : 'Not reachable right now';
      });
    }
  }

  $('add').addEventListener('submit', (e) => {
    e.preventDefault();
    const url = clean($('url').value);
    if (!url) {
      status('That needs to be an http:// or https:// address.', true);
      return;
    }
    const list = load().filter((s) => s.url !== url);
    list.unshift({ name: new URL(url).hostname, url });
    save(list);
    render();
    go(url);
  });

  render();
  // Straight back to the last server, unless the menu asked to switch.
  const last = localStorage.getItem(LAST);
  if (last && !new URLSearchParams(location.search).has('switch')) {
    probe(last).then((up) => { if (up) go(last); });
  }
})();

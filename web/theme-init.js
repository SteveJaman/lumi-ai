/* Applies the saved (or OS) theme before first paint. Loaded as a
   plain blocking <script> in <head> on every page, so dark-mode users
   don't see a flash of the light theme while app.js loads.

   data-theme is the light/dark MODE (only 'light' or 'dark'; anything
   else becomes 'dark'). The look (default / sakura / cyberpunk) is
   data-lumi-theme, set later by theme-engine.js, which falls back to
   'default' for an unknown id and marks <html data-theme-ready> when
   its stylesheet has settled. Nothing here waits for it. */
(function () {
  var theme = null;
  try { theme = localStorage.getItem('lumi-theme'); } catch (e) {}
  if (theme !== 'light' && theme !== 'dark') {
    theme = 'dark';   // the cyber look is dark-first; the sidebar toggle switches to daylight
  }
  document.documentElement.setAttribute('data-theme', theme);

  // Watchdog for the web app's wake-up screen (.lumi-boot): app.js marks
  // <html data-boot-js> as soon as it runs. If it never does (the module
  // failed to load or threw), don't leave the page behind the loader.
  setTimeout(function () {
    if (document.documentElement.hasAttribute('data-boot-js') || !document.body) return;
    var boot = document.getElementById('lumi-boot');
    if (!boot) return;
    boot.parentNode.removeChild(boot);
    document.body.classList.remove('lumi-booting');
  }, 10000);
})();

/* Applies the saved (or OS) theme before first paint. Loaded as a
   plain blocking <script> in <head> on every page, so dark-mode users
   don't see a flash of the light theme while app.js loads. */
(function () {
  var theme = null;
  try { theme = localStorage.getItem('lumi-theme'); } catch (e) {}
  if (theme !== 'light' && theme !== 'dark') {
    theme = 'dark';   // the cyber look is dark-first; the sidebar toggle switches to daylight
  }
  document.documentElement.setAttribute('data-theme', theme);
})();

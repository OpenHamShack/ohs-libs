// Fetches /api/hello with nothing but the launch cookie the window was
// given: if this fills in, the token exchange worked for scripts as well
// as for the page. Keys are shown as they come (they are identifiers).
(function () {
  fetch('api/hello').then(function (r) { return r.json(); }).then(function (info) {
    var dl = document.getElementById('facts');
    Object.keys(info).forEach(function (k) {
      var dt = document.createElement('dt');
      var dd = document.createElement('dd');
      dt.append(k);
      dd.append(String(info[k]));
      dl.append(dt, dd);
    });
    // Which shell is showing the page: pywebview injects its bridge
    // (window.pywebview) into its own windows only.
    var shell = document.createElement('dd');
    var label = document.createElement('dt');
    label.append('shell');
    shell.append(window.pywebview ? 'pywebview' : 'browser');
    dl.append(label, shell);
    window.addEventListener('pywebviewready', function () { shell.textContent = 'pywebview'; });
    document.getElementById('ok').hidden = false;
  });
})();

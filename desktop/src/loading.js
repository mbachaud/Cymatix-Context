'use strict';

// main.js passes progress through the URL hash: #status=...&error=...&splash=file:///...
const params = new URLSearchParams(window.location.hash.slice(1));
const status = params.get('status');
const error = params.get('error');
const splash = params.get('splash');
if (status) document.getElementById('status').textContent = status;
if (error) document.getElementById('error').textContent = error;
if (splash && splash.startsWith('file:')) {
  const img = document.getElementById('splash');
  img.src = splash;
  img.hidden = false;
}

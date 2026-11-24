const CACHE = 'xiyun-personal-static-v8';
const PERSONAL_ROOT = new URL(self.location.href).searchParams.has('personal-root');
const ASSETS = [
  '/personal/index.html', '/styles.css', '/assets/rehearsal.webp',
  '/personal/styles.css', '/personal/app.js', '/personal/media.js', '/personal/storage.js',
  '/personal/analysis.js', '/personal/analysis-worker.js', '/personal/knowledge.js',
  '/personal/manifest.webmanifest', '/personal/icon-192.png', '/personal/icon-512.png',
  '/src/audio.js', '/src/course.js'
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys
    .filter(key => key.startsWith('xiyun-personal-static-') && key !== CACHE)
    .map(key => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  const asset = PERSONAL_ROOT && url.pathname === '/' ? '/personal/index.html' : url.pathname;
  if (!ASSETS.includes(asset)) return;
  event.respondWith(caches.open(CACHE).then(cache => cache.match(asset))
    .then(response => response || fetch(event.request)));
});

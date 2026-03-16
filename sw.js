const CACHE_NAME = 'wave-picking-v1';
const API_CACHE = 'api-cache-v1';

// Samo fajlovi koje kesiramo
const urlsToCache = [
  'index_mobile.html',
  'manifest.json',
  'https://cdn.sheetjs.com/xlsx-0.20.2/package/dist/xlsx.full.min.js'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('Kes otvoren');
        return cache.addAll(urlsToCache).catch(err => {
          console.log('Kesiranje nije uspelo:', err);
        });
      })
  );
});

self.addEventListener('fetch', event => {
  // 1. Ignorisi sve sto nije GET zahtev
  if (event.request.method !== 'GET') {
    return;
  }
  
  // 2. Ignorisi chrome-extension i druge ne-http zahteve
  if (!event.request.url.startsWith('http')) {
    return;
  }

  // 3. Za API pozive - samo mreza, bez kesiranja
  if (event.request.url.includes('maga-codex.onrender.com')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // 4. Za staticke fajlove - cache first
  event.respondWith(
    caches.match(event.request)
      .then(response => {
        if (response) {
          return response;
        }
        return fetch(event.request).then(response => {
          if (!response || response.status !== 200) {
            return response;
          }
          const responseToCache = response.clone();
          caches.open(CACHE_NAME).then(cache => {
            cache.put(event.request, responseToCache);
          });
          return response;
        });
      })
  );
});

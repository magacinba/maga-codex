const CACHE_NAME = 'wave-picking-v1';
const API_CACHE = 'api-cache-v1';

// Fajlovi za kesiranje - samo oni koji sigurno postoje
const urlsToCache = [
  'index_mobile.html',
  'manifest.json',
  'https://cdn.sheetjs.com/xlsx-0.20.2/package/dist/xlsx.full.min.js'
];

// Instalacija service worker-a
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('Kes otvoren');
        // Filtriramo samo http/https URL-ove
        const validUrls = urlsToCache.filter(url =>
          url.startsWith('http') || url.startsWith('/')
        );
        return cache.addAll(validUrls).catch(err => {
          console.log('Kesiranje nije uspelo za neke fajlove:', err);
        });
      })
  );
});

// Aktivacija i ciscenje starog kesa
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cacheName => {
          if (cacheName !== CACHE_NAME && cacheName !== API_CACHE) {
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
});

// Strategija: Network first, pa cache (samo za http/https)
self.addEventListener('fetch', event => {
  // Ignorisi chrome-extension:// i druge ne-http zahteve
  if (!event.request.url.startsWith('http')) {
    return;
  }

  // Za API pozive - cache sa mrezom
  if (event.request.url.includes('maga-codex.onrender.com')) {
    event.respondWith(
      fetch(event.request)
        .then(response => {
          // Sacuvaj u cache samo validne response
          if (response && response.status === 200) {
            const responseClone = response.clone();
            caches.open(API_CACHE).then(cache => {
              cache.put(event.request, responseClone);
            });
          }
          return response;
        })
        .catch(() => {
          // Ako nema mreze, uzmi iz cache-a
          return caches.match(event.request);
        })
    );
  } else {
    // Za staticke fajlove - cache first
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
            // Sacuvaj u cache samo http response
            const responseToCache = response.clone();
            caches.open(CACHE_NAME).then(cache => {
              cache.put(event.request, responseToCache);
            });
            return response;
          });
        })
    );
  }
});

const CACHE_NAME = 'wave-picking-v1';
const API_CACHE = 'api-cache-v1';

// Samo fajlovi koje kesiramo - proveri da SVI postoje na GitHub-u
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
        // Filtriramo samo validne URL-ove
        const validUrls = urlsToCache.filter(url =>
          url.startsWith('http') || url.startsWith('/') || url.startsWith('index')
        );
        return cache.addAll(validUrls).catch(err => {
          console.log('Kesiranje nije uspelo za neke fajlove:', err);
          // Nastavi dalje cak i ako kesiranje ne uspe
        });
      })
  );
});

self.addEventListener('fetch', event => {
  // 1. Ignorisi sve sto nije GET zahtev (ovo je kljucno!)
  if (event.request.method !== 'GET') {
    return;
  }
  
  // 2. Ignorisi chrome-extension i druge ne-http zahteve
  if (!event.request.url.startsWith('http')) {
    return;
  }

  // 3. Za API pozive - samo mreza, NIKAKO kesiranje POST zahteva
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
          // Proveri da li je response validan pre kesiranja
          if (!response || response.status !== 200 || response.type !== 'basic') {
            return response;
          }
          const responseToCache = response.clone();
          caches.open(CACHE_NAME).then(cache => {
            cache.put(event.request, responseToCache).catch(err => {
              console.log('Kesiranje nije uspelo:', err);
            });
          });
          return response;
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

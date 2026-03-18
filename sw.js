const CACHE_NAME = 'wave-picking-v1';
const API_CACHE = 'api-cache-v1';

// Samo fajlovi koje keširamo
const urlsToCache = [
  'index_mobile.html',
  'manifest.json',
  'https://cdn.sheetjs.com/xlsx-0.20.2/package/dist/xlsx.full.min.js'
];

// Instalacija - keširaj samo osnovne fajlove
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('Keš otvoren');
        return cache.addAll(urlsToCache).catch(err => {
          console.log('Keširanje nije uspelo za neke fajlove:', err);
        });
      })
  );
});

// Fetch - PAMETNO rukovanje zahtevima
self.addEventListener('fetch', event => {
  const url = event.request.url;
  const method = event.request.method;

  // 1. Potpuno ignoriši POST, PUT, DELETE zahteve
  if (method !== 'GET') {
    return;
  }

  // 2. Ignoriši chrome-extension i data URL-ove
  if (!url.startsWith('http')) {
    return;
  }

  // 3. Za API pozive - NIKAD ne keširaj, uvek idi na mrežu
  if (url.includes('maga-codex.onrender.com')) {
    event.respondWith(
      fetch(event.request)
        .catch(() => {
          // Ako nema mreže, vrati jednostavan response
          return new Response(JSON.stringify({ error: 'Offline' }), {
            status: 503,
            headers: { 'Content-Type': 'application/json' }
          });
        })
    );
    return;
  }

  // 4. Za statičke fajlove - cache first
  event.respondWith(
    caches.match(event.request)
      .then(cachedResponse => {
        // Ako imamo u kešu, vrati iz keša
        if (cachedResponse) {
          return cachedResponse;
        }

        // Ako nema u kešu, idi na mrežu
        return fetch(event.request)
          .then(networkResponse => {
            // Proveri da li je response validan za keširanje
            if (!networkResponse || networkResponse.status !== 200) {
              return networkResponse;
            }

            // Sačuvaj samo ako je osnovni tip i GET zahtev
            const responseToCache = networkResponse.clone();
            caches.open(CACHE_NAME)
              .then(cache => {
                cache.put(event.request, responseToCache)
                  .catch(err => console.log('Keširanje nije uspelo:', err));
              });
            return networkResponse;
          })
          .catch(() => {
            // Ako nema mreže ni keša, vrati fallback
            if (url.includes('.html')) {
              return caches.match('index_mobile.html');
            }
            return new Response('Offline', { status: 503 });
          });
      })
  );
});

// Aktivacija - očisti stari keš
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cacheName => {
          if (cacheName !== CACHE_NAME && cacheName !== API_CACHE) {
            console.log('Brišem stari keš:', cacheName);
            return caches.delete(cacheName);
          }
        })
      );
    }).then(() => {
      console.log('Service Worker aktiviran');
      return self.clients.claim();
    })
  );
});
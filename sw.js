const CACHE_NAME = 'wave-picking-v1';
const API_CACHE = 'api-cache-v1';

// Fajlovi za kesiranje
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
        return cache.addAll(urlsToCache);
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

// Strategija: Network first, pa cache
self.addEventListener('fetch', event => {
  // Za API pozive - cache sa mrezom
  if (event.request.url.includes('maga-codex.onrender.com')) {
    event.respondWith(
      fetch(event.request)
        .then(response => {
          // Sacuvaj u cache
          const responseClone = response.clone();
          caches.open(API_CACHE).then(cache => {
            cache.put(event.request, responseClone);
          });
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
          return fetch(event.request).then(
            response => {
              if (!response || response.status !== 200 || response.type !== 'basic') {
                return response;
              }
              const responseToCache = response.clone();
              caches.open(CACHE_NAME)
                .then(cache => {
                  cache.put(event.request, responseToCache);
                });
              return response;
            }
          );
        })
    );
  }
});

// Sync za offline podatke
self.addEventListener('sync', event => {
  if (event.tag === 'sync-wave-data') {
    event.waitUntil(syncWaveData());
  }
});

async function syncWaveData() {
  try {
    const db = await openDB();
    const tx = db.transaction('pending-updates', 'readonly');
    const store = tx.objectStore('pending-updates');
    const updates = await store.getAll();

    for (const update of updates) {
      try {
        const response = await fetch(`${API}/wave/${update.session_id}/update`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(update.data)
        });

        if (response.ok) {
          const deleteTx = db.transaction('pending-updates', 'readwrite');
          const deleteStore = deleteTx.objectStore('pending-updates');
          await deleteStore.delete(update.id);
        }
      } catch (err) {
        console.log('Sync failed for update', update.id);
      }
    }
  } catch (err) {
    console.log('Sync error:', err);
  }
}

// IndexedDB helper
function openDB() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('WavePickingDB', 1);

    request.onupgradeneeded = event => {
      const db = event.target.result;
      if (!db.objectStoreNames.contains('pending-updates')) {
        db.createObjectStore('pending-updates', { keyPath: 'id', autoIncrement: true });
      }
    };

    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

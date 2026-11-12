const CACHE_NAME = 'multicanal-v39';
const ASSETS_TO_CACHE = [
  '/',
  '/index.html',
  '/manifest.json',
  '/html5-qrcode.min.js',
  '/logo-multicanal.png',
  '/icon-192.png',
  '/icon-512.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE).catch(err => {
        console.warn('SW cache.addAll warning:', err);
      });
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            console.log('SW eliminando caché antigua:', key);
            return caches.delete(key);
          }
        })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = e.request.url;

  // 1. Las llamadas API siempre van a la red
  if (url.includes('/api/')) {
    e.respondWith(
      fetch(e.request).catch(() => {
        return new Response(JSON.stringify({ offline: true, error: 'Sin conexión a internet' }), {
          headers: { 'Content-Type': 'application/json' }
        });
      })
    );
    return;
  }

  // 2. Navegación y HTML: Network-First (primero red para tener siempre la última versión, si no hay internet usa caché)
  if (e.request.mode === 'navigate' || e.request.destination === 'document' || url.endsWith('.html') || url.endsWith('/')) {
    e.respondWith(
      fetch(e.request).then((networkRes) => {
        if (networkRes && networkRes.status === 200) {
          const clone = networkRes.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(e.request, clone));
        }
        return networkRes;
      }).catch(() => caches.match('/index.html'))
    );
    return;
  }

  // 3. Imágenes y assets estáticos: Cache-First con fallback a red
  e.respondWith(
    caches.match(e.request).then((res) => {
      return res || fetch(e.request).then((netRes) => {
        if (netRes && netRes.status === 200 && e.request.method === 'GET') {
          const clone = netRes.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(e.request, clone));
        }
        return netRes;
      });
    })
  );
});

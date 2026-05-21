// SolarGuard PWA Service Worker
// Strategie:
//  - Statické (HTML, ikony, manifest) -> cache-first (rychlé načtení)
//  - /api/state -> network-first se stale fallback
//    (pokud network funguje => network odpoveď; pokud ne => last cached)
//  - /api/history, /api/decisions, /api/events -> network-only
//    (tyhle můžou být velké, nepotřebujeme je offline)

const VERSION = 'sg-v4.3.0-1';
const STATIC_CACHE = `${VERSION}-static`;
const STATE_CACHE = `${VERSION}-state`;

const STATIC_ASSETS = [
  '/',
  '/static/manifest.json',
  '/static/icon.svg',
  '/static/icon-180.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

// Install: precache statické věci
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// Activate: smazat staré cache verze
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => !k.startsWith(VERSION))
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// Fetch: ruzne strategie podle URL
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // POST/PUT/DELETE - nikdy necachuj, jen passthrough
  if (request.method !== 'GET') return;

  // /api/state - network-first se stale fallback
  if (url.pathname === '/api/state') {
    event.respondWith(networkFirstWithCache(request, STATE_CACHE));
    return;
  }

  // Ostatní /api/* - network-only (history, decisions, events, appliances)
  if (url.pathname.startsWith('/api/')) {
    return; // browser default
  }

  // Statické a HTML root - cache-first
  event.respondWith(cacheFirst(request, STATIC_CACHE));
});

async function cacheFirst(request, cacheName) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch (e) {
    // offline - fallback na cached root
    if (request.mode === 'navigate') {
      const root = await caches.match('/');
      if (root) return root;
    }
    throw e;
  }
}

async function networkFirstWithCache(request, cacheName) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch (e) {
    // Offline - vrat posledni znamy stav s flag stale=true
    const cached = await caches.match(request);
    if (cached) {
      // Pridej hlavicku ze je to stale
      const data = await cached.json();
      data._stale = true;
      data._stale_age_sec = data.ts ? Math.round(Date.now()/1000 - data.ts) : null;
      return new Response(JSON.stringify(data), {
        headers: { 'Content-Type': 'application/json' },
      });
    }
    throw e;
  }
}

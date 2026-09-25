/*
 * Sicherer PWA-Service-Worker:
 * - cachel nur die statische App-Hülle
 * - /status, /history und Steuernachrichten bleiben immer live im Netz
 */
const CACHE_NAME = 'wp-webapp-shell-v3';
const APP_SHELL = ['./', './index.html', './manifest.json'];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(APP_SHELL))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys()
            .then(keys => Promise.all(
                keys.filter(key => key !== CACHE_NAME)
                    .map(key => caches.delete(key))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', event => {
    if (event.request.method !== 'GET') return;
    const url = new URL(event.request.url);
    if (url.origin !== self.location.origin) return;

    // Diese Pfade niemals cachen, damit die Anzeige nicht veraltet.
    if (url.pathname === '/status' || url.pathname === '/health' ||
        url.pathname.startsWith('/history') || url.pathname === '/control' ||
        url.pathname === '/command' || url.pathname === '/config' ||
        url.pathname.startsWith('/debug/')) return;

    if (url.pathname === '/' || url.pathname.endsWith('.html') ||
        url.pathname === '/manifest.json') {
        // HTML/Manifest bevorzugt frisch vom Server laden. Bei Offline-Betrieb
        // dient der letzte bekannte App-Stand als Rückfallebene.
        event.respondWith(
            fetch(event.request, { cache: 'no-store' })
                .then(response => {
                    const copy = response.clone();
                    caches.open(CACHE_NAME)
                        .then(cache => cache.put(event.request, copy));
                    return response;
                })
                .catch(() => caches.match(event.request))
        );
    }
});

/*
 * Sicherer PWA-Service-Worker:
 * - cachel nur die statische App-Hülle
 * - /status, /history und Steuernachrichten bleiben immer live im Netz
 */
// v8 (24.10.2026): Hintergrund-Polling auf 30 s gedrosselt - die Huelle
// muss beim naechsten Besuch neu geladen werden.
const CACHE_NAME = 'wp-webapp-shell-v8';
const APP_SHELL = [
    './',
    './index.html',
    './manifest.json',
    // Chart.js + Zoom-Plugin liegen lokal im Repo (webapp/vendor/). Sie
    // gehoeren zur Huelle: ohne sie fehlt der Temperaturverlauf, und ein
    // zweiter Netzversuch waere noetig - genau das Symptom "laedt erst beim
    // 2. Anlauf". Die Pfade muessen relativ bleiben, damit sie unter
    // demselben Scope wie die Seite liegen.
    './vendor/chart.umd.min.js',
    './vendor/chartjs-plugin-zoom.min.js',
];

// Ein einzelnes fehlendes Asset hat frueher die KOMPLETTE Installation
// gesprengt: cache.addAll() ist alles-oder-nichts und lehnt mit TypeError ab,
// sobald eine Antwort keinen 200er-Status hat (404 oder 429). Firefox meldete
// dann "ServiceWorker script ... encountered an error during installation"
// (17.10.2026). Jetzt wird jede Datei einzeln gecacht - die Huelle bleibt
// nutzbar, auch wenn ein optionales Asset fehlt.
function cacheAppShell(cache) {
    return Promise.all(APP_SHELL.map(url =>
        cache.add(url).catch(err =>
            console.warn('App-Shell nicht gecacht:', url, err)
        )
    ));
}

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cacheAppShell(cache))
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
        url.pathname.startsWith('/analysis/') || url.pathname.startsWith('/debug/')) return;

    if (url.pathname === '/' || url.pathname.endsWith('.html') ||
        url.pathname === '/manifest.json') {
        // HTML/Manifest bevorzugt frisch vom Server laden. Bei Offline-Betrieb
        // dient der letzte bekannte App-Stand als Rueckfallebene.
        event.respondWith(
            fetch(event.request, { cache: 'no-store' })
                .then(response => {
                    // NUR erfolgreiche Antworten cachen. Vorher landete jede
                    // Antwort im Cache - auch eine 429 (Rate-Limit) oder ein
                    // 503 ("System not initialized"). Damit war die
                    // Offline-Huelle nach einem einzigen Fehlversuch
                    // vergiftet: der Browser lieferte danach die
                    // Fehlerseite statt der WebApp. Nachgewiesen im
                    // Headless-Browser: cacheStatus 429 statt 200.
                    if (response && response.ok) {
                        const copy = response.clone();
                        caches.open(CACHE_NAME)
                            .then(cache => cache.put(event.request, copy));
                    }
                    return response;
                })
                .catch(() => caches.match(event.request))
        );
        return;
    }

    // Vendor-Bibliotheken (Chart.js, Zoom-Plugin): cache-first. Die Dateien
    // liegen lokal im Repo und aendern sich nur mit dem Deployment - dafuer
    // sorgt der Cache-Name. Ohne diesen Zweig waeren sie offline nicht
    // verfuegbar und der Temperaturverlauf fehlte.
    if (url.pathname.startsWith('/vendor/')) {
        event.respondWith(
            caches.match(event.request).then(cached => {
                if (cached) return cached;
                return fetch(event.request).then(response => {
                    if (response && response.ok) {
                        const copy = response.clone();
                        caches.open(CACHE_NAME)
                            .then(cache => cache.put(event.request, copy));
                    }
                    return response;
                });
            })
        );
    }
});

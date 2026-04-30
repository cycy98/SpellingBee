const CACHE = 'sb-v2';

self.addEventListener('install', e =>
    e.waitUntil(caches.open(CACHE).then(c => c.addAll([
        'static/styles.css',
        'static/apple-touch-icon.png',
        'static/icon-192.png',
        'static/icon-512.png',
    ]))));

self.addEventListener('activate', e =>
    e.waitUntil(caches.keys().then(ks =>
        Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))));

self.addEventListener('fetch', e => {
    const { url } = e.request;
    if (!url.startsWith(self.location.origin)) return; // CDN: let browser handle
    if (url.includes('/stream')) return;               // SSE: never intercept
    if (url.includes('/static/') || url.includes('/audios/')) {
        // Cache-first; populate cache dynamically on miss
        e.respondWith(
            caches.match(e.request).then(r => {
                if (r) return r;
                return fetch(e.request).then(res => {
                    caches.open(CACHE).then(c => c.put(e.request, res.clone()));
                    return res;
                });
            })
        );
        return;
    }
    // Everything else: network-first, serve cache on failure
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request).then(r => r ?? Response.error())));
});

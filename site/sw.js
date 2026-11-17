// Makes Video XP installable as an app (Brave / Chrome need a service worker that handles requests).
// Always the network first, so feed.json / state.json are never stale; the cached shell is only a fallback offline.
const CACHE = "vxp-v1";
const ROOT = new URL("./", self.location).pathname;
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll([ROOT, ROOT + "manifest.webmanifest", ROOT + "icon-192.png", ROOT + "icon-512.png"])));
  self.skipWaiting();
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || !url.pathname.startsWith(ROOT)) return;   // YouTube, relay: untouched
  e.respondWith(fetch(req).then(res => {
    if (res.ok && (req.mode === "navigate" || /\.(png|webmanifest)$/.test(url.pathname))) {
      const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy));
    }
    return res;
  }).catch(() => caches.match(req).then(hit => hit || caches.match(ROOT))));
});

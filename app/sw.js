// Keeps the dashboard shell available offline. API calls always go to the
// local service (never cached), so data is never stale.
const CACHE = "dday-shell-v1";
const SHELL = ["/app/", "/app/app.js", "/app/app.css", "/app/base.css", "/app/manifest.webmanifest",
  "/app/icons/icon32.png", "/app/icons/icon48.png", "/app/icons/icon128.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/") || e.request.method !== "GET") return;
  // Network first (always latest UI when the service runs), cache as fallback.
  e.respondWith(fetch(e.request).then((resp) => {
    const copy = resp.clone();
    caches.open(CACHE).then((c) => c.put(e.request, copy));
    return resp;
  }).catch(() => caches.match(e.request, { ignoreSearch: true })));
});

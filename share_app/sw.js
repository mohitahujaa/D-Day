// Makes the share app installable and lets it open offline (messages are then queued).
const CACHE = "dday-share-v2";
const SHELL = ["./", "./index.html", "./app.js", "./style.css", "./manifest.webmanifest", "./icon192.png", "./icon512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return; // Google APIs: never cached
  // Shares arrive as ./?text=...; always serve the app shell for the start page.
  const isPage = e.request.mode === "navigate";
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(isPage ? "./index.html" : e.request, copy));
        return resp;
      })
      .catch(() => caches.match(isPage ? "./index.html" : e.request, { ignoreSearch: true }))
  );
});

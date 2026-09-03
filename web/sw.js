/* Bella PWA service worker — cache only the app shell so it opens offline.
   Never caches API / WebSocket traffic. */
var SHELL = "bella-shell-v1";
var ASSETS = ["/", "/index.html", "/styles.css", "/app.js", "/manifest.json", "/icon.svg"];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(SHELL).then(function (c) { return c.addAll(ASSETS); }));
  self.skipWaiting();
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== SHELL; })
                            .map(function (k) { return caches.delete(k); }));
    })
  );
  self.clients.claim();
});

self.addEventListener("fetch", function (e) {
  var url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  // never intercept the API surface
  if (["/ws", "/message", "/health", "/status", "/devices"].indexOf(url.pathname) !== -1) return;
  if (ASSETS.indexOf(url.pathname) === -1) return;
  e.respondWith(
    fetch(e.request).then(function (r) {
      var copy = r.clone();
      caches.open(SHELL).then(function (c) { c.put(e.request, copy); });
      return r;
    }).catch(function () { return caches.match(e.request); })
  );
});

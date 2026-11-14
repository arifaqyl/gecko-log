// Offline cache and reminders. When you change any file, bump VERSION here AND the ?v= on
// register('sw.js?v=…') in index.html, so phones (and Cloudflare's cache) pick up the new worker.
const VERSION = 'gecko-v7';
const FILES = ['./', './manifest.json', './icon-192.png', './icon-512.png', './apple-touch-icon.png'];

self.addEventListener('install', e => {
  // Cache each file on its own, so one missing file can't stop the app installing.
  e.waitUntil(caches.open(VERSION)
    .then(c => Promise.all(FILES.map(f => fetch(f, {cache:'reload'}).then(r => r.ok && c.put(f, r)).catch(() => {}))))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Network first so updates show up, cache as fallback when offline. The sync API is never cached.
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET' || e.request.url.includes('/gecko-api/')) return;
  e.respondWith(
    fetch(e.request)
      .then(res => {
        if (res.ok || res.type === 'opaque'){ const copy = res.clone(); caches.open(VERSION).then(c => c.put(e.request, copy)); }
        return res;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match('./')))
  );
});

// Reminders sent by the Pi.
self.addEventListener('push', e => {
  let msg = {title:'Gecko Log', body:'Something is due today.', url:'./'};
  try { msg = Object.assign(msg, e.data.json()); } catch(err){}
  e.waitUntil(self.registration.showNotification(msg.title, {
    body: msg.body, icon: 'icon-192.png', badge: 'icon-192.png', tag: 'gecko-daily', renotify: true, data: {url: msg.url}
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || './', self.registration.scope).href;
  e.waitUntil(self.clients.matchAll({type:'window', includeUncontrolled:true}).then(list => {
    const open = list.find(c => c.url.startsWith(self.registration.scope));
    if (open){ open.focus(); return open.navigate(url); }
    return self.clients.openWindow(url);
  }));
});

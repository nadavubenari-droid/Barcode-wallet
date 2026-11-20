// Lets the app be installed to the home screen. It doesn't cache anything,
// so updates to the site always show up straight away.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});

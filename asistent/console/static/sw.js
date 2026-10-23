/* 事务台 service worker：只缓存**静态壳**，永不缓存 API。
 *
 * 为什么：待办/追问/审批必须每次从持久化存储取，缓存住一个"看起来还有待办"的旧页面
 * 恰恰是 SPEC §9.4 要避免的错觉。API 一律 network-only，失败就让页面显示"离线，
 * 显示上一次结果"，而不是拿缓存冒充最新状态。
 *
 * 注意：service worker 需要**安全上下文**（HTTPS 或 localhost）。手机经 frp 走
 * `https://<域名>/` 时可用；直接用 `http://<公网IP>:端口/` 打开时浏览器会拒绝注册，
 * 页面功能不受影响（PWA 安装是加分项，不是前提）。
 */
'use strict';

// 缓存名里带版本：换皮肤/换图标时必须升版本，否则手机会一直用旧壳
var SHELL_CACHE = 'asistent-console-shell-v3';
var SHELL_FILES = [
  '/', '/index.html', '/styles.css', '/app.js', '/manifest.webmanifest',
  '/icon.svg', '/icon-192.png', '/icon-512.png', '/icon-maskable-512.png', '/apple-touch-icon.png'
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      return cache.addAll(SHELL_FILES).catch(function () { /* 单个文件缺失不阻塞安装 */ });
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (key) {
        return key === SHELL_CACHE ? null : caches.delete(key);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (event) {
  var request = event.request;
  var url = new URL(request.url);
  if (request.method !== 'GET' || url.pathname.indexOf('/api/') === 0) {
    return; // API 与写请求：不拦截、不缓存（network only）
  }
  event.respondWith(
    fetch(request).then(function (response) {
      var copy = response.clone();
      caches.open(SHELL_CACHE).then(function (cache) { cache.put(request, copy); })
        .catch(function () { });
      return response;
    }).catch(function () {
      return caches.match(request).then(function (cached) {
        return cached || caches.match('/index.html');
      });
    })
  );
});

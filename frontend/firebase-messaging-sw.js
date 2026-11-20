/* eslint-disable no-undef */
// Service Worker for PWA offline shell + Firebase Cloud Messaging (FCM) background notifications
importScripts("https://www.gstatic.com/firebasejs/10.12.5/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/10.12.5/firebase-messaging-compat.js");

// Điền cùng cấu hình Firebase Web App như trong frontend/firebase-config.js
const SW_FIREBASE_CONFIG = {
  apiKey: "AIzaSyAsZ8cXc5UwLkvW2nLoqZz0KaOOioGLo1E",
  authDomain: "bottrader-e12ec.firebaseapp.com",
  projectId: "bottrader-e12ec",
  storageBucket: "bottrader-e12ec.firebasestorage.app",
  messagingSenderId: "746147835498",
  appId: "1:746147835498:web:0022bc06718788ebee2413",
  measurementId: "G-FREWZGJ6W6"
};

const CACHE_NAME = "vn-swing-bot-v1";
const SHELL_ASSETS = [
  "./",
  "./index.html",
  "./styles.css",
  "./app.js",
  "./firebase-config.js",
  "./manifest.json"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});

if (!SW_FIREBASE_CONFIG.apiKey.startsWith("REPLACE_")) {
  firebase.initializeApp(SW_FIREBASE_CONFIG);
  const messaging = firebase.messaging();

  messaging.onBackgroundMessage((payload) => {
    const title = payload.notification?.title || "VN Stock Swing Bot";
    const options = {
      body: payload.notification?.body || "Có cập nhật mới từ hệ thống.",
      icon: "./icons/icon-192.svg",
      badge: "./icons/icon-192.svg",
      data: payload.data || {}
    };
    self.registration.showNotification(title, options);
  });
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ("focus" in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow("./index.html");
    })
  );
});

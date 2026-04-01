self.addEventListener("install", function () {
    self.skipWaiting();
});

self.addEventListener("activate", function (event) {
    event.waitUntil(self.clients.claim());
});

self.addEventListener("push", function (event) {
    if (!event.data) {
        return;
    }

    const payload = event.data.json();
    const title = payload.title || "Family Coordination App";
    const options = {
        body: payload.body || "",
        icon: "/static/favicon.png",
        badge: "/static/favicon.png",
        data: {
            url: payload.url || "/"
        }
    };

    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
    event.notification.close();

    event.waitUntil(
        self.clients.matchAll({type: "window", includeUncontrolled: true}).then(function (clients) {
            const targetUrl = event.notification.data && event.notification.data.url
                ? event.notification.data.url
                : "/";

            for (const client of clients) {
                if ("focus" in client) {
                    client.navigate(targetUrl);
                    return client.focus();
                }
            }

            if (self.clients.openWindow) {
                return self.clients.openWindow(targetUrl);
            }
        })
    );
});

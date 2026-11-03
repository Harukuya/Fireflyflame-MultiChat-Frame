// Only injected by the loopback HTTP adapter; embedded hosts keep their own bridge.
(() => {
  if (window.MultiChatPage || window.AstrBotPluginPage) return;
  const token = document.querySelector('meta[name="multichat-local-token"]')?.content;
  if (!token) return;
  window.MultiChatPage = {
    async request({ method, path, params = {}, body = {} }) {
      const url = new URL(`/api/${path}`, window.location.origin);
      if (method === "GET") for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
      const response = await fetch(url, {
        method, credentials: "same-origin", cache: "no-store",
        headers: { Authorization: `Bearer ${token}`, ...(method === "POST" ? { "Content-Type": "application/json" } : {}) },
        ...(method === "POST" ? { body: JSON.stringify(body) } : {}),
      });
      const result = await response.json();
      if (!response.ok && result?.ok !== false) throw new Error(`本地仪表盘请求失败（${response.status}）`);
      return result;
    },
  };
})();

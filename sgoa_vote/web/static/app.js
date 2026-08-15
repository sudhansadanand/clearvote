/* Shared helpers. Deliberately small and dependency-free: no framework is
   loaded, so nothing can be fetched from the internet at runtime. */

const SGOA = {
  csrf: (document.body && document.body.dataset.csrf) || "",

  /* Empty when this meeting is served at the root, or "/<event>" when several
     meetings are served from one process. Every absolute path in the page goes
     through url() or go() so a page never reaches into another event. */
  base: (document.body && document.body.dataset.base) || "",

  url(path) {
    return (path && path.charAt(0) === "/") ? SGOA.base + path : path;
  },

  go(path) {
    window.location.href = SGOA.url(path);
  },

  async api(method, url, body) {
    const opts = {
      method,
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
    };
    if (SGOA.csrf) opts.headers["X-CSRF-Token"] = SGOA.csrf;
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(SGOA.url(url), opts);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const err = new Error((data && data.message) || ("Request failed (" + res.status + ")"));
      err.status = res.status;
      err.payload = data || {};
      throw err;
    }
    return data;
  },

  get(url) { return SGOA.api("GET", url); },
  post(url, body) { return SGOA.api("POST", url, body); },
  patch(url, body) { return SGOA.api("PATCH", url, body); },

  el(id) { return document.getElementById(id); },

  text(id, value) {
    const node = SGOA.el(id);
    if (node) node.textContent = value;
  },

  show(id, visible) {
    const node = SGOA.el(id);
    if (node) node.style.display = visible ? "" : "none";
  },

  /* RFC 4122 v4 without a library; used for the vote idempotency key. */
  uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map(b => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  },

  escape(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  },

  banner(containerId, message, nextAction, kind) {
    const node = SGOA.el(containerId);
    if (!node) return;
    if (!message) { node.innerHTML = ""; return; }
    node.innerHTML =
      '<div class="notice notice-' + (kind || "error") + '">' +
      SGOA.escape(message) +
      (nextAction ? '<span class="next-action">' + SGOA.escape(nextAction) + "</span>" : "") +
      "</div>";
  },
};

// Same-origin CSRF plumbing shared by the product SPA, server-rendered pages, and
// the pinned vendored Anki frontend. The token remains bound to the server session.
function cookie(name: string): string {
  const prefix = encodeURIComponent(name) + "=";
  for (const part of document.cookie.split(";")) {
    const value = part.trim();
    if (value.startsWith(prefix)) return decodeURIComponent(value.slice(prefix.length));
  }
  return "";
}

function unsafe(method: string): boolean {
  return !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase());
}

const nativeFetch = window.fetch.bind(window);
window.fetch = (input: RequestInfo | URL, init: RequestInit = {}) => {
  const source = input instanceof Request ? input : null;
  const method = String(init.method || source?.method || "GET").toUpperCase();
  const target = new URL(source?.url || String(input), location.href);
  if (target.origin === location.origin && unsafe(method)) {
    const token = cookie("ankiweb_csrf");
    if (token) {
      const headers = new Headers(init.headers || source?.headers);
      headers.set("X-CSRF-Token", token);
      init = { ...init, headers };
    }
  }
  return nativeFetch(input, init);
};

const xhrOpen = XMLHttpRequest.prototype.open;
const xhrSend = XMLHttpRequest.prototype.send;
const methodKey = Symbol("ankiweb-method");
const urlKey = Symbol("ankiweb-url");
XMLHttpRequest.prototype.open = function(method: string, url: string | URL, ...rest: any[]) {
  (this as any)[methodKey] = method;
  (this as any)[urlKey] = String(url);
  return xhrOpen.call(this, method, url, ...rest as [boolean?, string?, string?]);
};
XMLHttpRequest.prototype.send = function(body?: Document | XMLHttpRequestBodyInit | null) {
  const method = String((this as any)[methodKey] || "GET");
  const target = new URL(String((this as any)[urlKey] || location.href), location.href);
  const token = cookie("ankiweb_csrf");
  if (token && target.origin === location.origin && unsafe(method)) {
    this.setRequestHeader("X-CSRF-Token", token);
  }
  return xhrSend.call(this, body);
};

window.addEventListener("DOMContentLoaded", () => {
  const token = cookie("ankiweb_csrf");
  if (!token) return;
  document.querySelectorAll<HTMLFormElement>("form[method]").forEach((form) => {
    if ((form.method || "GET").toUpperCase() !== "POST") return;
    const action = new URL(form.action || location.href, location.href);
    if (action.origin !== location.origin) return;
    let field = form.querySelector<HTMLInputElement>('input[name="_csrf"]');
    if (!field) {
      field = document.createElement("input");
      field.type = "hidden";
      field.name = "_csrf";
      form.appendChild(field);
    }
    field.value = token;
  });
});

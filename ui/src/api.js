// Tiny fetch wrapper. Every call is reported to `onCall` so the UI can show the raw exchange.
let onCall = () => {};
export const setCallLogger = (fn) => (onCall = fn);

export async function api(method, path, { body, headers } = {}) {
  const started = performance.now();
  const res = await fetch(path, {
    method,
    headers: { ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* empty body */
  }
  const replayed = res.headers.get("Idempotent-Replayed") === "true";
  onCall({
    method,
    path,
    requestHeaders: headers,
    requestBody: body,
    status: res.status,
    replayed,
    data,
    ms: Math.round(performance.now() - started),
    at: new Date().toLocaleTimeString(),
  });
  if (!res.ok) {
    const e = data?.error || { code: "HTTP_" + res.status, message: res.statusText };
    throw Object.assign(new Error(e.message), { code: e.code, details: e.details, status: res.status });
  }
  return { data, replayed };
}

export const money = (cents) => (cents == null ? "-" : `$${(cents / 100).toFixed(2)}`);
export const newKey = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());

const fs = require("node:fs");
const path = require("node:path");
const net = require('node:net');
const original = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
  const opts = Array.isArray(args[0]) ? args[0][0] : args[0];
  const host = typeof opts === "object" ? opts.host || opts.hostname : typeof args[1] === "string" ? args[1] : undefined;
  if (host && !['127.0.0.1','localhost','::1'].includes(host)) throw new Error('Harness restricted to local relay: ' + host);
  return original.apply(this, args);
};


const originalFetch = globalThis.fetch.bind(globalThis);
const endpoint = process.env.SIM_BIM_LOCAL_ENDPOINT;
let dispatcher;
let localFetch;
globalThis.fetch = function (input, init) {
  const url = String(input && typeof input === "object" && "url" in input ? input.url : input);
  if (url !== endpoint) return originalFetch(input, init);
  if (!dispatcher) {
    const library = path.join(process.resourcesPath, "app.asar", "dsh", "node_modules", "undici");
    const { Agent, fetch } = require(library);
    localFetch = fetch;
    const transportTimeoutMs = 1530000;
    dispatcher = new Agent({ headersTimeout: transportTimeoutMs, bodyTimeout: transportTimeoutMs,
                             connect: { timeout: 20000 } });
    fs.writeFileSync(process.env.SIM_BIM_LOCAL_TRANSPORT_RECEIPT,
      JSON.stringify({ providerUrlMatched: true, headersTimeoutMs: transportTimeoutMs,
        bodyTimeoutMs: transportTimeoutMs, connectTimeoutMs: 20000,
        bundledUndiciVersion: require(path.join(library, "package.json")).version }), { flag: "wx" });
  }
  return localFetch(input, { ...init, dispatcher });
};

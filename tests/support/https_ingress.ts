/**
 * Owned loopback HTTPS ingress reverse proxy for Voice AI browser qualification.
 *
 * Serves web UI and platform API from a single HTTPS origin, routing /api/v1
 * directly to platform_api and all other paths to Next.js web studio.
 */

import { parseArgs } from "util";

const { values } = parseArgs({
  args: Bun.argv.slice(2),
  options: {
    port: { type: "string" },
    api: { type: "string" },
    web: { type: "string" },
    cert: { type: "string" },
    key: { type: "string" },
  },
});

const port = parseInt(values.port || "8443", 10);
const apiBase = (values.api || "http://127.0.0.1:8000").replace(/\/+$/, "");
const webBase = (values.web || "http://127.0.0.1:3000").replace(/\/+$/, "");
const certPath = values.cert;
const keyPath = values.key;

if (!certPath || !keyPath) {
  console.error("Missing required --cert or --key argument for HTTPS ingress");
  process.exit(1);
}

const server = Bun.serve({
  port,
  hostname: "127.0.0.1",
  tls: {
    cert: Bun.file(certPath),
    key: Bun.file(keyPath),
  },
  async fetch(req) {
    const url = new URL(req.url);

    // Dedicated ingress health endpoint for fast readiness checking
    if (url.pathname === "/health/ingress") {
      return Response.json({ status: "ok", ingress: true });
    }

    // Same-origin routing:
    // Route /api/v1 and /api/v1/* directly to platform_api
    // Route all other requests to Next.js web studio
    const isApi = url.pathname === "/api/v1" || url.pathname.startsWith("/api/v1/");
    const targetBase = isApi ? apiBase : webBase;
    const targetUrl = new URL(url.pathname + url.search, targetBase);
    const fwdHeaders = new Headers(req.headers);
    // Request identity encoding from upstream so body matches stream bytes exactly
    fwdHeaders.set("accept-encoding", "identity");
    fwdHeaders.set("host", new URL(targetBase).host);
    fwdHeaders.set("x-forwarded-proto", "https");
    fwdHeaders.set("x-forwarded-host", url.host);
    const existingFwd = fwdHeaders.get("x-forwarded-for");
    fwdHeaders.set("x-forwarded-for", existingFwd ? `${existingFwd}, 127.0.0.1` : "127.0.0.1");

    // Remove hop-by-hop headers from upstream request
    fwdHeaders.delete("connection");
    fwdHeaders.delete("keep-alive");
    fwdHeaders.delete("transfer-encoding");
    fwdHeaders.delete("upgrade");

    try {
      const resp = await fetch(targetUrl, {
        method: req.method,
        headers: fwdHeaders,
        body: ["GET", "HEAD"].includes(req.method) ? undefined : req.body,
        redirect: "manual",
      });

      const respHeaders = new Headers(resp.headers);
      // Strip upstream transfer/compression framing headers that do not match the decompressed body stream
      respHeaders.delete("content-encoding");
      respHeaders.delete("content-length");
      respHeaders.delete("transfer-encoding");
      respHeaders.delete("connection");
      respHeaders.delete("keep-alive");

      const nullBodyStatuses = [204, 205, 304];
      const body = nullBodyStatuses.includes(resp.status) ? null : resp.body;

      return new Response(body, {
        status: resp.status,
        statusText: resp.statusText,
        headers: respHeaders,
      });
    } catch (err: unknown) {
      console.error(`Ingress proxy error to ${targetUrl}:`, err);
      return new Response("Bad Gateway", { status: 502 });
    }
  },
});

console.log(`HTTPS Ingress proxy listening on https://127.0.0.1:${server.port}`);

// Clean termination handlers
const shutdown = () => {
  server.stop(true);
  process.exit(0);
};

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

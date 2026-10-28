// @vitest-environment jsdom
// The shared setup uses window; these checks use real HTTP servers, not DOM mocks.
import { createServer as createHttpServer, request, type IncomingHttpHeaders, type Server, type ServerResponse } from "node:http";
import { once } from "node:events";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { createServer as createViteServer, type ViteDevServer } from "vite";

import viteConfig, { remoteDevelopmentProxy } from "../../vite.config";

interface ReceivedRequest {
  path: string;
  method: string;
  headers: IncomingHttpHeaders;
  body: string;
}

interface HttpResult {
  status: number;
  headers: IncomingHttpHeaders;
  body: string;
}

function send(origin: string, path: string, headers: Record<string, string> = {}, body?: string): Promise<HttpResult> {
  return new Promise((resolve, reject) => {
    const outgoing = request(origin, { path, headers, method: body === undefined ? "GET" : "POST" }, (response) => {
      const chunks: Buffer[] = [];
      response.on("data", (chunk: Buffer) => chunks.push(chunk));
      response.on("end", () => resolve({ status: response.statusCode!, headers: response.headers, body: Buffer.concat(chunks).toString() }));
      response.on("error", reject);
    });
    outgoing.on("error", reject);
    outgoing.end(body);
  });
}

async function listen(server: Server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("HTTP test server has no port");
  return `http://127.0.0.1:${address.port}`;
}

async function close(server: Server) {
  server.closeAllConnections();
  await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}

describe("localhost remote development proxy", () => {
  let upstream: Server;
  let browser: Server;
  let vite: ViteDevServer;
  let upstreamOrigin: string;
  let browserOrigin: string;
  let stream: ServerResponse | undefined;
  const received: ReceivedRequest[] = [];

  beforeAll(async () => {
    upstream = createHttpServer(async (incoming, response) => {
      const chunks: Buffer[] = [];
      for await (const chunk of incoming) chunks.push(Buffer.from(chunk));
      received.push({ path: incoming.url!, method: incoming.method!, headers: incoming.headers, body: Buffer.concat(chunks).toString() });
      if (incoming.url === "/api/auth/guest") {
        if (incoming.headers.origin !== upstreamOrigin || incoming.headers["sec-fetch-site"] !== "same-origin"
          || incoming.headers.host !== new URL(upstreamOrigin).host) {
          response.writeHead(403).end();
          return;
        }
        response.setHeader("Set-Cookie", [
          "seekino_auth=guest-token; Domain=remote.invalid; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=600",
          "tracking=discarded; Path=/",
        ]);
      }
      if (incoming.url === "/api/videos/content?videoId=one") {
        response.writeHead(206, { "Content-Range": "bytes 2-4/8", "Content-Type": "video/mp4" });
        response.end("234");
        return;
      }
      if (incoming.url === "/api/events") {
        stream = response;
        response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
        response.write("event: ready\ndata: first\n\n");
        return;
      }
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify(received.at(-1)));
    });
    upstreamOrigin = await listen(upstream);
    const remote = remoteDevelopmentProxy(upstreamOrigin);
    vite = await createViteServer({
      configFile: false,
      root: process.cwd(),
      logLevel: "silent",
      plugins: [remote.plugin],
      server: { middlewareMode: true, hmr: false, watch: null, proxy: { "^/remote(?:/|\\?|$)": remote.options } },
    });
    browser = createHttpServer(vite.middlewares);
    browserOrigin = await listen(browser);
  });

  afterEach(() => vi.unstubAllEnvs());
  afterAll(async () => {
    stream?.end();
    await close(browser);
    await vite.close();
    await close(upstream);
  });

  it("keeps the remote optional and defines the fixed development connection only when configured", async () => {
    vi.stubEnv("SEEKINO_REMOTE_API_URL", "");
    vi.stubEnv("VITE_REMOTE_API_BASE_URL", "");
    vi.stubEnv("VITE_REMOTE_API_ORIGIN", "");
    if (typeof viteConfig !== "function") throw new Error("Vite config must resolve environment values");
    const local = await viteConfig({ command: "serve", mode: "test" });
    expect(local.define?.["import.meta.env.VITE_REMOTE_API_BASE_URL"]).toBe('""');
    expect(local.server?.proxy?.["/api"]).toEqual({ target: expect.stringMatching(/^http:\/\/127\.0\.0\.1:/), changeOrigin: false });
    expect(local.server?.proxy?.["/healthz"]).toEqual(local.server?.proxy?.["/api"]);

    vi.stubEnv("SEEKINO_REMOTE_API_URL", upstreamOrigin);
    const configured = await viteConfig({ command: "serve", mode: "test" });
    expect(configured.define?.["import.meta.env.VITE_REMOTE_API_BASE_URL"]).toBe('"/remote"');
    expect(configured.define?.["import.meta.env.VITE_REMOTE_API_ORIGIN"]).toBe(JSON.stringify(upstreamOrigin));
    const production = await viteConfig({ command: "build", mode: "test" });
    expect(production.define?.["import.meta.env.VITE_REMOTE_API_BASE_URL"]).toBe('""');
    vi.stubEnv("VITE_REMOTE_API_BASE_URL", "/account-server");
    expect((await viteConfig({ command: "build", mode: "test" })).define?.["import.meta.env.VITE_REMOTE_API_BASE_URL"])
      .toBe('"/account-server"');
  });

  it("rejects unsafe upstream configuration", () => {
    for (const target of ["http://remote.example", "https://remote.example/api", "https://user:password@remote.example", "https://remote.example?target=other", "https://remote.example/#fragment"]) {
      expect(() => remoteDevelopmentProxy(target)).toThrow("SEEKINO_REMOTE_API_URL");
    }
    expect(() => remoteDevelopmentProxy("https://remote.example", "invalid cookie")).toThrow("SEEKINO_REMOTE_COOKIE_NAME");
    expect(remoteDevelopmentProxy("https://remote.example").options.target).toBe("https://remote.example");
  });

  it("creates a guest with upstream origin checks and scopes only the remote session cookie", async () => {
    const result = await send(browserOrigin, "/remote/api/auth/guest", {
      Origin: browserOrigin, "Sec-Fetch-Site": "same-origin", Cookie: "seekino_auth=local-token; unrelated=private",
      Authorization: "Bearer local-credential", "X-Forwarded-Host": "other.example", "X-Forwarded-Proto": "https",
    }, "{}");
    expect(result.status).toBe(200);
    const upstreamRequest = JSON.parse(result.body) as ReceivedRequest;
    expect(upstreamRequest.headers.origin).toBe(upstreamOrigin);
    expect(upstreamRequest.headers.cookie).toBeUndefined();
    expect(upstreamRequest.headers.authorization).toBeUndefined();
    expect(upstreamRequest.headers["x-forwarded-host"]).toBeUndefined();
    expect(upstreamRequest.headers["x-forwarded-proto"]).toBeUndefined();
    const cookies = result.headers["set-cookie"]!;
    expect(cookies).toHaveLength(1);
    expect(cookies[0]).toMatch(/^seekino_remote_[a-f0-9]{16}=guest-token;/);
    expect(cookies[0]).toContain("HttpOnly");
    expect(cookies[0]).toContain("SameSite=Lax");
    expect(cookies[0]).toContain("Max-Age=600");
    expect(cookies[0]).toContain("Path=/remote");
    expect(cookies[0]).not.toMatch(/(?:Domain=|Secure|tracking)/i);
  });

  it("forwards the remote session, CSRF, idempotency, content range and body without local credentials", async () => {
    const guest = await send(browserOrigin, "/remote/api/auth/guest", { Origin: browserOrigin }, "{}");
    const remoteCookie = guest.headers["set-cookie"]![0]!.split(";")[0]!;
    const result = await send(browserOrigin, "/remote/api/uploads/parts?partNumber=2", {
      Origin: browserOrigin, Cookie: `seekino_auth=local-token; ${remoteCookie}; another=private`,
      "X-CSRF-Token": "remote-csrf", "Idempotency-Key": "upload-key", "Content-Range": "bytes 2-4/8",
    }, "234");
    expect(result.status).toBe(200);
    const forwarded = JSON.parse(result.body) as ReceivedRequest;
    expect(forwarded.path).toBe("/api/uploads/parts?partNumber=2");
    expect(forwarded.method).toBe("POST");
    expect(forwarded.body).toBe("234");
    expect(forwarded.headers.cookie).toBe("seekino_auth=guest-token");
    expect(forwarded.headers["x-csrf-token"]).toBe("remote-csrf");
    expect(forwarded.headers["idempotency-key"]).toBe("upload-key");
    expect(forwarded.headers["content-range"]).toBe("bytes 2-4/8");
  });

  it("does not reuse cookies from a different fixed upstream", async () => {
    const guest = await send(browserOrigin, "/remote/api/auth/guest", { Origin: browserOrigin }, "{}");
    const remoteCookie = guest.headers["set-cookie"]![0]!.split(";")[0]!;
    const originalOrigin = new URL(upstreamOrigin);
    originalOrigin.hostname = "localhost";
    const different = remoteDevelopmentProxy(originalOrigin.origin);
    const alternateVite = await createViteServer({
      configFile: false, root: process.cwd(), logLevel: "silent",
      plugins: [different.plugin], server: { middlewareMode: true, hmr: false, watch: null, proxy: { "^/remote(?:/|\\?|$)": different.options } },
    });
    const alternateBrowser = createHttpServer(alternateVite.middlewares);
    try {
      const origin = await listen(alternateBrowser);
      const result = await send(origin, "/remote/api/auth/me", { Cookie: remoteCookie });
      expect(result.status).toBe(200);
      expect((JSON.parse(result.body) as ReceivedRequest).headers.cookie).toBeUndefined();
    } finally {
      await close(alternateBrowser);
      await alternateVite.close();
    }
  });

  it("denies cross-origin ingress and paths outside remote API/health before reaching upstream", async () => {
    const count = received.length;
    const deniedHeaders: Record<string, string>[] = [
      { Origin: "https://other.example" },
      { Origin: "null" },
      { Origin: "not-an-origin" },
      { Origin: `${browserOrigin}/extra` },
      { Origin: browserOrigin, "Sec-Fetch-Site": "cross-site" },
      { "Sec-Fetch-Site": "same-site" },
    ];
    for (const headers of deniedHeaders) {
      expect((await send(browserOrigin, "/remote/api/auth/guest", headers, "{}")).status).toBe(403);
    }
    for (const path of ["/remote", "/remote/private", "/remote/api/../private", "/remote/api/%2e%2e/private", "/remote/api/%5c..%5cprivate", "/remote/healthz/private"]) {
      expect((await send(browserOrigin, path)).status).toBe(404);
    }
    expect(received).toHaveLength(count);
    const health = await send(browserOrigin, "/remote/healthz");
    expect(health.status).toBe(200);
    expect((JSON.parse(health.body) as ReceivedRequest).path).toBe("/healthz");
  });

  it("preserves video range requests and partial response headers", async () => {
    const result = await send(browserOrigin, "/remote/api/videos/content?videoId=one", { Range: "bytes=2-4" });
    expect(result.status).toBe(206);
    expect(result.headers["content-range"]).toBe("bytes 2-4/8");
    expect(result.body).toBe("234");
    expect(received.at(-1)?.headers.range).toBe("bytes=2-4");
  });

  it("delivers an SSE frame before the upstream response ends", async () => {
    const outgoing = request(browserOrigin, { path: "/remote/api/events" });
    const incomingPromise = once(outgoing, "response");
    outgoing.end();
    const [response] = await incomingPromise as [import("node:http").IncomingMessage];
    const [chunk] = await once(response, "data") as [Buffer];
    expect(chunk.toString()).toBe("event: ready\ndata: first\n\n");
    expect(response.headers["content-type"]).toBe("text/event-stream");
    expect(stream?.writableEnded).toBe(false);
    const ended = once(response, "end");
    stream!.end();
    response.resume();
    await ended;
  });
});

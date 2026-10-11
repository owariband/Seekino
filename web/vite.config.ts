import { createHash } from "node:crypto";
import { defineConfig, loadEnv, type Plugin, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = `http://${process.env.ANIVAG_HTTP_HOST || "127.0.0.1"}:${process.env.ANIVAG_HTTP_PORT || "9621"}`;

export function remoteDevelopmentProxy(targetValue: string, upstreamCookie = "anivag_auth") {
  const target = new URL(targetValue);
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(target.hostname);
  if ((target.protocol !== "https:" && !(target.protocol === "http:" && loopback))
    || target.username || target.password || target.pathname !== "/" || target.search || target.hash) {
    throw new Error("ANIVAG_REMOTE_API_URL must be an HTTPS origin (HTTP is allowed only for loopback development)");
  }
  if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$/.test(upstreamCookie)) {
    throw new Error("ANIVAG_REMOTE_COOKIE_NAME must be a valid session cookie name");
  }
  const cookieScope = createHash("sha256").update(`${target.origin}:${upstreamCookie}`).digest("hex").slice(0, 16);
  const browserCookie = `anivag_remote_${cookieScope}`;
  const plugin: Plugin = {
    name: "anivag-remote-development-proxy",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        if (!/^\/remote(?:\/|\?|$)/.test(request.url || "")) return next();
        let pathAllowed = false;
        let sameOrigin = false;
        try {
          const incoming = new URL(request.url!, `http://${request.headers.host}`);
          const rawPath = request.url!.split("?")[0];
          const decodedPath = decodeURIComponent(incoming.pathname);
          pathAllowed = incoming.pathname === rawPath
            && !decodedPath.includes("\\")
            && !decodedPath.split("/").some((part) => part === "." || part === "..")
            && (incoming.pathname === "/remote/api" || incoming.pathname.startsWith("/remote/api/")
              || incoming.pathname === "/remote/healthz");
          sameOrigin = ["localhost", "127.0.0.1", "[::1]"].includes(incoming.hostname)
            && Number(incoming.port || 80) === request.socket.localPort;
          if (request.headers.origin) {
            const origin = new URL(request.headers.origin);
            sameOrigin &&= origin.origin === incoming.origin && !origin.username && !origin.password
              && origin.pathname === "/" && !origin.search && !origin.hash;
          }
          const site = request.headers["sec-fetch-site"];
          sameOrigin &&= !site || site === "same-origin" || site === "none";
        } catch {
          // Reject malformed paths and origins before sending any session upstream.
          sameOrigin = false;
        }
        if (!pathAllowed || !sameOrigin) {
          response.statusCode = pathAllowed ? 403 : 404;
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify({ message: pathAllowed ? "远端连接仅接受本站请求" : "远端接口不存在" }));
          return;
        }
        next();
      });
    },
  };
  const options: ProxyOptions = {
    target: target.origin,
    changeOrigin: true,
    rewrite: (path) => path.slice("/remote".length),
    configure(proxy) {
      proxy.on("proxyReq", (outgoing, request) => {
        for (const header of outgoing.getHeaderNames()) {
          if (["cookie", "authorization", "proxy-authorization", "forwarded", "x-real-ip"].includes(header)
            || header.startsWith("x-forwarded-")) outgoing.removeHeader(header);
        }
        const session = (request.headers.cookie || "").split(";")
          .map((cookie) => cookie.trim()).find((cookie) => cookie.startsWith(`${browserCookie}=`));
        if (session) outgoing.setHeader("Cookie", `${upstreamCookie}=${session.slice(browserCookie.length + 1)}`);
        outgoing.setHeader("Host", target.host);
        outgoing.setHeader("Origin", target.origin);
        outgoing.setHeader("Referer", `${target.origin}/`);
        outgoing.setHeader("Sec-Fetch-Site", "same-origin");
      });
      proxy.on("proxyRes", (upstream) => {
        const cookies = (upstream.headers["set-cookie"] || []).flatMap((cookie) => {
          const [value, ...attributes] = cookie.split(";").map((part) => part.trim());
          if (!value?.startsWith(`${upstreamCookie}=`)) return [];
          // The browser talks to an HTTP loopback server; upstream TLS remains verified.
          const retained = attributes.filter((attribute) => !/^(?:domain|path|secure)(?:=|$)/i.test(attribute));
          if (!retained.some((attribute) => /^httponly$/i.test(attribute))) retained.push("HttpOnly");
          return [`${browserCookie}=${value.slice(upstreamCookie.length + 1)}; ${retained.join("; ")}; Path=/remote`];
        });
        if (cookies.length) upstream.headers["set-cookie"] = cookies;
        else delete upstream.headers["set-cookie"];
      });
    },
  };
  return { plugin, options, origin: target.origin };
}

export default defineConfig(({ command, mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), ""), ...process.env };
  const remote = command === "serve" && env.ANIVAG_REMOTE_API_URL?.trim()
    ? remoteDevelopmentProxy(env.ANIVAG_REMOTE_API_URL.trim(), env.ANIVAG_REMOTE_COOKIE_NAME || "anivag_auth")
    : undefined;
  return {
    plugins: [react(), ...(remote ? [remote.plugin] : [])],
    define: {
      "import.meta.env.VITE_REMOTE_API_BASE_URL": JSON.stringify(remote ? "/remote" : env.VITE_REMOTE_API_BASE_URL || ""),
      "import.meta.env.VITE_REMOTE_API_ORIGIN": JSON.stringify(remote?.origin || env.VITE_REMOTE_API_ORIGIN || ""),
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": apiTarget,
        "/healthz": apiTarget,
        ...(remote ? { "^/remote(?:/|\\?|$)": remote.options } : {}),
      },
    },
    preview: {
      host: "127.0.0.1",
      port: 4173,
    },
  };
});

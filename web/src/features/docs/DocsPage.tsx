import { useQuery } from "@tanstack/react-query";
import { Check, Copy, Search } from "lucide-react";
import { useLayoutEffect, useRef, useState, type PropsWithChildren } from "react";
import Markdown, { defaultUrlTransform, type Components } from "react-markdown";
import { Link, Navigate, NavLink, useParams } from "react-router";
import remarkGfm from "remark-gfm";

import { useI18n } from "../../app/i18n";
import { GitHubMark } from "./GitHubMark";
import { GITHUB_URL } from "./links";
import styles from "./docs.module.css";

function useGuideFile(path: string) {
  return useQuery({
    queryKey: ["user-guide", path],
    queryFn: async ({ signal }) => {
      const response = await fetch(path, { signal, headers: { Accept: "text/markdown" } });
      if (response.status === 404) return null;
      if (!response.ok || response.headers.get("Content-Type")?.includes("text/html")) {
        throw new Error(`Unable to read Markdown at ${path} (HTTP ${response.status})`);
      }
      return response.text();
    },
    staleTime: Infinity,
    retry: false,
  });
}

// Resolve references against the Markdown file, just as GitHub does. Article links open in the app.
function guideUrl(raw: string, source: string, localeRoot: string) {
  const safe = defaultUrlTransform(raw);
  const base = new URL(source, window.location.origin);
  if (!safe || !URL.canParse(safe, base)) return "";
  const url = new URL(safe, base);
  if (url.origin !== window.location.origin) return url.href;
  if (url.pathname.startsWith(localeRoot) && url.pathname.endsWith(".md")) {
    const slug = url.pathname.slice(localeRoot.length, -3);
    const path = slug === "start" || slug === "README" ? "/docs" : `/docs/${slug}`;
    return path + url.search + url.hash;
  }
  return url.pathname + url.search + url.hash;
}

function CodeBlock({ children }: PropsWithChildren) {
  const { t } = useI18n();
  const pre = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);
  async function copy() {
    if (!pre.current) return;
    try {
      await navigator.clipboard.writeText((pre.current.textContent ?? "").replace(/\n$/, ""));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch { /* The command can still be selected and copied manually. */ }
  }
  return <div className={styles.code}>
    <pre ref={pre}>{children}</pre>
    <button type="button" onClick={copy} aria-label={t(copied ? "已复制" : "复制")}>
      {copied ? <Check size={15} /> : <Copy size={15} />}
    </button>
  </div>;
}

const articleElements: Components = {
  a: ({ href, children }) => href?.startsWith("/") && !href.startsWith("/docs/guide/") ? <Link to={href}>{children}</Link>
    : <a href={href} target="_blank" rel="noreferrer">{children}</a>,
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
  code: ({ children, className }) => {
    const origin = window.location.origin;
    // The example stays portable on GitHub; Cloud visitors copy their site's actual URL.
    const text = typeof children === "string" && /^https:\/\/[a-z0-9.-]+(?::[0-9]+)?$/i.test(origin)
      ? children.replaceAll("https://your-seekino-cloud.example", origin) : children;
    return <code className={className}>{text}</code>;
  },
  table: ({ children }) => <div className={styles.tableWrap}><table>{children}</table></div>,
  img: ({ src, alt, title }) => <img src={src} alt={alt} title={title} loading="lazy" />,
};

function StartHeading({ children }: PropsWithChildren) {
  const { t } = useI18n();
  return <>
    <h1>{children}</h1>
    <div className={styles.actions}>
      <Link className="button-primary" to="/search" state={{ focusComposer: true }}><Search size={16} />{t("现在试一句")}</Link>
      <a className="button-secondary" href={GITHUB_URL} target="_blank" rel="noreferrer"><GitHubMark size={17} />{t("GitHub 上给个 Star")}</a>
    </div>
  </>;
}

const startElements: Components = { ...articleElements, h1: StartHeading };

const navigationElements: Components = {
  h1: ({ children }) => <p>{children}</p>,
  a: ({ href, children }) => <NavLink to={href ?? "/docs"} end>{children}</NavLink>,
};

export function DocsPage() {
  const { locale, t } = useI18n();
  const { "*": route = "" } = useParams();
  const slug = route.replace(/\/+$/, "") || "start";
  const localeRoot = `/docs/guide/${locale}/`;
  const parts = slug.split("/");
  const valid = parts.every(part => part.length > 0 && !part.startsWith(".") && !part.includes("\\"));
  const indexPath = localeRoot + "README.md";
  const pagePath = `${localeRoot}${valid ? parts.map(encodeURIComponent).join("/") : "start"}.md`;
  const index = useGuideFile(indexPath);
  const page = useGuideFile(pagePath);
  const ref = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    for (let el = ref.current?.parentElement; el; el = el.parentElement) {
      if (/(auto|scroll)/.test(getComputedStyle(el).overflowY)) {
        el.scrollTop = 0;
        break;
      }
    }
  }, [slug]);

  if (!valid || (route && slug === "start") || (page.data === null && slug !== "start")) return <Navigate to="/docs" replace />;
  const failed = index.isError || page.isError || index.data === null || page.data === null;
  return <main className={styles.docs} ref={ref}>
    <nav className={styles.toc} aria-label={t("文档")}>
      <Markdown components={navigationElements} urlTransform={url => guideUrl(url, indexPath, localeRoot)}>{index.data}</Markdown>
    </nav>
    <article className={styles.article}>
      {failed ? <div role="alert">
        <p>{t("无法加载使用文档")}</p>
        <button type="button" onClick={() => { void index.refetch(); void page.refetch(); }}>{t("重试")}</button>
      </div> : page.isPending ? <p role="status">{t("正在加载…")}</p> :
        <Markdown remarkPlugins={[remarkGfm]} components={slug === "start" ? startElements : articleElements}
          urlTransform={url => guideUrl(url, pagePath, localeRoot)}>{page.data}</Markdown>
      }
    </article>
  </main>;
}

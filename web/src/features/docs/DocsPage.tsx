import { ArrowRight, Check, Copy, ExternalLink, Search } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";
import { Link, Navigate, NavLink, useParams } from "react-router";

import { useI18n } from "../../app/i18n";
import { DOC_PAGES, docsContent, GITHUB_URL, type DocBlock, type DocPageId, type DocsContent } from "./content";
import { GitHubMark } from "./GitHubMark";
import styles from "./docs.module.css";

const pagePath = (page: DocPageId) => page === "start" ? "/docs" : `/docs/${page}`;

function CodeBlock({ code, copy, copied }: { code: string; copy: string; copied: string }) {
  const [done, setDone] = useState(false);
  return <div className={styles.code}>
    <pre><code>{code}</code></pre>
    <button type="button" onClick={async () => {
      try { await navigator.clipboard.writeText(code); setDone(true); window.setTimeout(() => setDone(false), 1600); }
      catch { /* Selecting the text still works when the clipboard is blocked. */ }
    }} aria-label={done ? copied : copy} title={done ? copied : copy}>
      {done ? <Check size={15} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}
    </button>
  </div>;
}

function Block({ block, content }: { block: DocBlock; content: DocsContent }) {
  switch (block.kind) {
    case "p": return <p className={styles.text}>{block.text}</p>;
    case "h": return <h2 className={styles.heading}>{block.text}</h2>;
    case "question": return <p className={styles.question}>{block.text}</p>;
    case "steps": return <ol className={styles.steps}>{block.items.map((step) => <li key={step.title}>
      <h3>{step.title}</h3>
      <p>{step.body}</p>
      {step.code ? <CodeBlock code={step.code} copy={content.copy} copied={content.copied} /> : null}
    </li>)}</ol>;
    // A record of one real search, not a control: nothing here looks pressable.
    case "result": return <figure className={styles.result}>
      <p className={styles.resultLabel}>{block.label}</p>
      <dl>
        <div><dt>{block.asked}</dt><dd className={styles.resultQuery}>“{block.query}”</dd></div>
        <div><dt>{block.found}</dt><dd className={styles.resultFound}><span>{block.source}</span><span className={styles.resultRange}>{block.range}</span><span className={styles.resultLength}>{block.length}</span></dd></div>
      </dl>
      <figcaption>{block.note}</figcaption>
    </figure>;
    case "examples": return <div className={styles.examples}>
      <p>{block.label}</p>
      <ul>{block.items.map((item) => <li key={item.text}><span>“{item.text}”</span><small>{item.note}</small></li>)}</ul>
    </div>;
    case "compare": return <div className={styles.tableWrap}>
      <table className={styles.compare}>
        <thead><tr>{block.columns.map((column, index) => <th key={index} scope="col">{column}</th>)}</tr></thead>
        <tbody>{block.rows.map(([label, cloud, local]) => <tr key={label}>
          <th scope="row">{label}</th><td data-label={block.columns[1]}>{cloud}</td><td data-label={block.columns[2]}>{local}</td>
        </tr>)}</tbody>
      </table>
    </div>;
    case "tools": return <dl className={styles.tools}>{block.items.map((tool) => <div key={tool.name}>
      <dt><code>{tool.name}</code></dt><dd>{tool.body}</dd>
    </div>)}</dl>;
    case "roadmap": return <ul className={styles.roadmap}>{block.items.map((item) => <li key={item.title}>
      <div><h3>{item.title}</h3><span>{block.status}</span></div>
      <p>{item.body}</p>
    </li>)}</ul>;
    case "channels": return <dl className={styles.channels}>{block.items.map((channel) => <div key={channel.label}>
      <dt>{channel.label}</dt>
      <dd>{channel.href ? <a href={channel.href} target="_blank" rel="noreferrer">{channel.value}</a> : channel.value}</dd>
    </div>)}</dl>;
    case "star": return <a className={styles.star} href={GITHUB_URL} target="_blank" rel="noreferrer">
      <GitHubMark size={22} />
      <span><strong>owariband/Seekino</strong><small>{block.text}</small></span>
      <ArrowRight size={18} aria-hidden="true" />
    </a>;
    case "links": return <div className={styles.links}>
      <p>{block.label}</p>
      <ul>{block.items.map((link) => <li key={link.href}>
        <a href={link.href} target="_blank" rel="noreferrer">{link.label}<ExternalLink size={13} aria-hidden="true" /></a>
      </li>)}</ul>
    </div>;
    case "next": return <nav className={styles.next} aria-label={content.nextLabel}>{block.items.map((item) => <Link key={item.page} to={pagePath(item.page)}>
      <span>{item.label}</span><ArrowRight size={16} aria-hidden="true" />
    </Link>)}</nav>;
  }
}

function scrollParent(node: HTMLElement | null): HTMLElement | null {
  for (let el = node?.parentElement ?? null; el; el = el.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(el).overflowY)) return el;
  }
  return null;
}

export function DocsPage() {
  const { locale } = useI18n();
  const params = useParams();
  const ref = useRef<HTMLElement>(null);
  const page = (params.page ?? "start") as DocPageId;
  const valid = DOC_PAGES.includes(page);
  // Pages share one scroll container with the app; a new page starts at its top.
  useLayoutEffect(() => {
    const container = valid ? scrollParent(ref.current) : null;
    if (container) container.scrollTop = 0;
  }, [page, valid]);
  if (!valid) return <Navigate to="/docs" replace />;
  const content = docsContent(locale);
  const current = content.pages[page];

  return <main className={styles.docs} ref={ref}>
    <nav className={styles.toc} aria-label={content.toc}>
      <p>{content.kicker}</p>
      <div>{DOC_PAGES.map((id) => <NavLink key={id} to={pagePath(id)} end className={({ isActive }) => isActive ? styles.tocActive : undefined}>
        {content.pages[id].nav}
      </NavLink>)}</div>
    </nav>

    <article className={styles.article}>
      <header className={styles.hero}>
        <p className="page-kicker">{current.nav}</p>
        <h1>{current.title}</h1>
        <p className={styles.lead}>{current.lead}</p>
        {page === "start" ? <div className={styles.actions}>
          <Link className="button-primary" to="/search" state={{ focusComposer: true }}><Search size={16} aria-hidden="true" />{content.tryCloud}</Link>
          <a className="button-secondary" href={GITHUB_URL} target="_blank" rel="noreferrer"><GitHubMark size={17} />{content.github}</a>
        </div> : null}
      </header>
      <div className={styles.body}>{current.blocks.map((block, index) => <Block key={index} block={block} content={content} />)}</div>
    </article>
  </main>;
}

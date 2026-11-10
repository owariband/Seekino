import { ArrowRight, Check, Copy, ExternalLink } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { useI18n } from "../../app/i18n";
import { docsContent, GITHUB_URL } from "./content";
import { GitHubMark } from "./GitHubMark";
import styles from "./docs.module.css";

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

export function DocsPage() {
  const { locale } = useI18n();
  const content = docsContent(locale);
  const { versions, usage, deploy, roadmap, community } = content.sections;
  const toc = [
    ["versions", versions.title], ["usage", usage.title], ["deploy", deploy.title],
    ["roadmap", roadmap.title], ["community", community.title],
  ] as const;

  return <main className={styles.docs}>
    <aside className={styles.toc} aria-label={content.toc}>
      <p>{content.toc}</p>
      <nav>{toc.map(([id, title]) => <a key={id} href={`#${id}`}>{title}</a>)}</nav>
    </aside>

    <article className={styles.article}>
      <header className={styles.hero}>
        <p className="page-kicker">{content.kicker}</p>
        <h1>{content.title}</h1>
        <p className={styles.lead}>{content.lead}</p>
        <div className={styles.actions}>
          <Link className="button-primary" to="/watch">{content.tryCloud}<ArrowRight size={16} aria-hidden="true" /></Link>
          <a className="button-secondary" href={GITHUB_URL} target="_blank" rel="noreferrer"><GitHubMark size={17} />{content.github}</a>
        </div>
      </header>

      <section id="versions" className={styles.section}>
        <h2>{versions.title}</h2>
        <p>{versions.intro}</p>
        <div className={styles.tableWrap}>
          <table className={styles.compare}>
            <thead><tr>{versions.columns.map((column, index) => <th key={index} scope="col">{column}</th>)}</tr></thead>
            <tbody>{versions.rows.map(([label, cloud, local]) => <tr key={label}>
              <th scope="row">{label}</th><td data-label={versions.columns[1]}>{cloud}</td><td data-label={versions.columns[2]}>{local}</td>
            </tr>)}</tbody>
          </table>
        </div>
        <p className={styles.note}>{versions.note}</p>
      </section>

      <section id="usage" className={styles.section}>
        <h2>{usage.title}</h2>
        <p>{usage.intro}</p>
        <div className={styles.uses}>{usage.items.map((item) => <div key={item.title} className={styles.use}>
          <h3>{item.title}</h3>
          <p>{item.body}</p>
          <span>{item.where}</span>
        </div>)}</div>
      </section>

      <section id="deploy" className={styles.section}>
        <h2>{deploy.title}</h2>
        <p>{deploy.intro}</p>
        <ol className={styles.steps}>{deploy.steps.map((step) => <li key={step.title}>
          <h3>{step.title}</h3>
          <p>{step.body}</p>
          {step.code ? <CodeBlock code={step.code} copy={content.copy} copied={content.copied} /> : null}
        </li>)}</ol>
        <h3 className={styles.subhead}>{deploy.mcpTitle}</h3>
        <p>{deploy.mcpBody}</p>
        <CodeBlock code={deploy.mcpCode} copy={content.copy} copied={content.copied} />
        <p className={styles.more}>{deploy.more}</p>
        <ul className={styles.links}>{deploy.links.map((link) => <li key={link.href}>
          <a href={link.href} target="_blank" rel="noreferrer">{link.label}<ExternalLink size={13} aria-hidden="true" /></a>
        </li>)}</ul>
      </section>

      <section id="roadmap" className={styles.section}>
        <h2>{roadmap.title}</h2>
        <p>{roadmap.intro}</p>
        <ul className={styles.roadmap}>{roadmap.items.map((item) => <li key={item.title}>
          <div><h3>{item.title}</h3><span>{roadmap.status}</span></div>
          <p>{item.body}</p>
        </li>)}</ul>
      </section>

      <section id="community" className={styles.section}>
        <h2>{community.title}</h2>
        <p>{community.intro}</p>
        <dl className={styles.channels}>{community.channels.map((channel) => <div key={channel.label}>
          <dt>{channel.label}</dt>
          <dd>{channel.href ? <a href={channel.href} target="_blank" rel="noreferrer">{channel.value}</a> : channel.value}</dd>
        </div>)}</dl>
        <a className={styles.star} href={GITHUB_URL} target="_blank" rel="noreferrer">
          <GitHubMark size={22} />
          <span><strong>owariband/Seekino</strong><small>{community.closing}</small></span>
          <ArrowRight size={18} aria-hidden="true" />
        </a>
      </section>
    </article>
  </main>;
}

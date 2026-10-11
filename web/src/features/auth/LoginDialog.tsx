import { X } from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { errorMessage } from "../../api/client";
import { useAuth } from "./AuthProvider";
import styles from "./auth.module.css";

export function LoginDialog() {
  const { loginOpen, closeLogin, sendCode, verifyCode, accountIdentity } = useAuth();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [sentEmail, setSentEmail] = useState<string | null>(null);
  const [retryAt, setRetryAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const requestVersion = useRef(0);
  const remaining = Math.max(0, Math.ceil((retryAt - now) / 1000));

  useEffect(() => {
    ++requestVersion.current;
    setBusy(false);
    setCode(""); setSentEmail(null); setError(null);
  }, [accountIdentity?.userId]);

  useEffect(() => {
    if (!loginOpen) return;
    setNow(Date.now());
    const previous = document.activeElement as HTMLElement | null;
    panelRef.current?.querySelector<HTMLInputElement>("input")?.focus();
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") { closeLogin(); return; }
      if (event.key !== "Tab") return;
      const controls = Array.from(panelRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled)") ?? []);
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", keyboard);
    return () => {
      ++requestVersion.current;
      setBusy(false);
      document.removeEventListener("keydown", keyboard);
      previous?.focus();
    };
  }, [loginOpen, closeLogin]);

  useEffect(() => {
    if (!loginOpen || retryAt <= Date.now()) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [loginOpen, retryAt]);

  if (!loginOpen) return null;

  const send = async () => {
    const normalized = email.trim().toLowerCase();
    if (busy || remaining || !panelRef.current?.querySelector<HTMLInputElement>("input[type=email]")?.reportValidity()) return;
    const version = ++requestVersion.current;
    setBusy(true);
    setError(null);
    try {
      const seconds = await sendCode(normalized);
      if (version !== requestVersion.current) return;
      setSentEmail(normalized);
      setCode("");
      setRetryAt(Date.now() + seconds * 1000);
      setNow(Date.now());
    } catch (failure) {
      if (version === requestVersion.current) setError(errorMessage(failure));
    } finally {
      if (version === requestVersion.current) setBusy(false);
    }
  };

  const verify = async (event: FormEvent) => {
    event.preventDefault();
    const normalized = email.trim().toLowerCase();
    if (busy || sentEmail !== normalized || !/^\d{6}$/.test(code)) return;
    const version = ++requestVersion.current;
    setBusy(true);
    setError(null);
    try { await verifyCode(normalized, code); }
    catch (failure) {
      if (version === requestVersion.current) setError(errorMessage(failure));
    } finally {
      if (version === requestVersion.current) setBusy(false);
    }
  };

  return <div className={styles.backdrop} onClick={(event) => { if (event.target === event.currentTarget) closeLogin(); }}>
    <div ref={panelRef} className={styles.dialog} role="dialog" aria-modal="true" aria-labelledby="login-heading">
      <header>
        <div><h2 id="login-heading">邮箱登录</h2><p>登录后可访问你的远端资料。本机资料仍保留在本机。</p></div>
        <button className={styles.close} type="button" onClick={closeLogin} aria-label="关闭登录"><X size={18} /></button>
      </header>
      <form onSubmit={(event) => void verify(event)}>
        <label><span>邮箱</span><input className="field" type="email" autoComplete="email" required value={email}
          onChange={(event) => { setEmail(event.target.value); setError(null); }} /></label>
        <button className="button-secondary" type="button" disabled={busy || remaining > 0} onClick={() => void send()}>
          {remaining > 0 ? `${remaining} 秒后重新发送` : sentEmail ? "重新发送验证码" : "发送验证码"}
        </button>
        {sentEmail ? <p className={styles.notice} role="status">验证码已发送至 {sentEmail}</p> : null}
        <label><span>验证码</span><input className="field" type="text" inputMode="numeric" autoComplete="one-time-code"
          pattern="[0-9]{6}" maxLength={6} required value={code}
          onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} placeholder="6 位验证码" /></label>
        {error ? <p className={styles.error} role="alert">{error}</p> : null}
        <button className="button-primary" type="submit" disabled={busy || sentEmail !== email.trim().toLowerCase() || code.length !== 6}>
          {busy ? "处理中…" : "登录"}
        </button>
        <button className={styles.guestLink} type="button" onClick={closeLogin}>继续使用</button>
      </form>
    </div>
  </div>;
}

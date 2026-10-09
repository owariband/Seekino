import { useQueryClient } from "@tanstack/react-query";
import { createContext, type PropsWithChildren, useCallback, useContext, useEffect, useRef, useState } from "react";

import { ApiClientError, configureApiIdentity, createGuestIdentity, errorMessage, getAuthIdentity, logoutIdentity, sendEmailCode, verifyEmailCode, TENANT_ID } from "../../api/client";
import type { AuthIdentity } from "../../api/contracts";
import { LoginDialog } from "./LoginDialog";
import styles from "./auth.module.css";

interface AuthContextValue {
  identity: AuthIdentity;
  activeTenantId: string;
  scopeKey: string;
  canUpload: boolean;
  canManage: boolean;
  canConfigure: boolean;
  loginOpen: boolean;
  openLogin: () => void;
  closeLogin: () => void;
  selectTenant: (tenantId: string) => void;
  sendCode: (email: string) => Promise<number>;
  verifyCode: (email: string, code: string) => Promise<void>;
  logout: () => Promise<void>;
}

const localIdentity: AuthIdentity = {
  userId: "local", email: null, isGuest: false, tenantId: TENANT_ID,
  publicTenantId: TENANT_ID, mode: "local", csrfToken: "",
  permissions: { canUpload: true, canManage: true, canConfigure: true },
};
const unavailable = async () => { throw new Error("登录服务尚未初始化"); };
const AuthContext = createContext<AuthContextValue>({
  identity: localIdentity, activeTenantId: TENANT_ID, scopeKey: "local",
  canUpload: true, canManage: true, canConfigure: true, loginOpen: false,
  openLogin: () => undefined, closeLogin: () => undefined, selectTenant: () => undefined,
  sendCode: unavailable, verifyCode: unavailable, logout: unavailable,
});

export function AuthProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient();
  const [identity, setIdentity] = useState<AuthIdentity | null>(null);
  const [activeTenantId, setActiveTenantId] = useState<string | null>(null);
  const [loginOpen, setLoginOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const current = useRef<{ identity: AuthIdentity; tenantId: string } | null>(null);
  const refreshVersion = useRef(0);
  const refreshing = useRef<{ version: number; signal?: AbortSignal } | null>(null);
  const openLogin = useCallback(() => setLoginOpen(true), []);
  const closeLogin = useCallback(() => setLoginOpen(false), []);

  const applyIdentity = useCallback((next: AuthIdentity, tenantId?: string) => {
    const previous = current.current;
    const ownTenant = next.isGuest ? next.publicTenantId : next.tenantId;
    if (!ownTenant) throw new Error("当前身份缺少可访问的资料来源");
    const selected = tenantId ?? (previous?.identity.userId === next.userId ? previous.tenantId : ownTenant);
    const allowed = selected === next.publicTenantId || (!next.isGuest && selected === next.tenantId);
    const nextTenant = allowed && selected ? selected : ownTenant;
    const changed = !previous || previous.identity.userId !== next.userId
      || previous.identity.csrfToken !== next.csrfToken || previous.tenantId !== nextTenant;
    if (changed) {
      configureApiIdentity(next, nextTenant);
      void queryClient.cancelQueries();
      queryClient.clear();
      setRevision((value) => value + 1);
    }
    current.current = { identity: next, tenantId: nextTenant };
    setActiveTenantId(nextTenant);
    setIdentity(next);
    setError(null);
  }, [queryClient]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (refreshing.current && !refreshing.current.signal?.aborted) return;
    const version = ++refreshVersion.current;
    refreshing.current = { version, signal };
    try {
      let next: AuthIdentity;
      try { next = await getAuthIdentity(signal); }
      catch (failure) {
        if (!(failure instanceof ApiClientError) || failure.status !== 401) throw failure;
        next = await createGuestIdentity(signal);
      }
      if (signal?.aborted || version !== refreshVersion.current) return;
      applyIdentity(next);
    } catch (failure) {
      if (!signal?.aborted && version === refreshVersion.current) setError(errorMessage(failure));
    } finally {
      if (refreshing.current?.version === version) refreshing.current = null;
    }
  }, [applyIdentity]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    const onFocus = () => { void refresh(controller.signal); };
    window.addEventListener("focus", onFocus);
    window.addEventListener("anivag-auth-required", onFocus);
    return () => {
      controller.abort();
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("anivag-auth-required", onFocus);
    };
  }, [refresh]);

  if (!identity || !activeTenantId) {
    return <div className={styles.loading} role={error ? "alert" : "status"}>
      <p>{error ? `暂时无法连接登录服务：${error}` : "正在连接服务…"}</p>
      {error ? <button className="button-primary" type="button" onClick={() => void refresh()}>重试连接</button> : null}
    </div>;
  }

  const writable = identity.mode === "local" || (!identity.isGuest && activeTenantId === identity.tenantId);
  const value: AuthContextValue = {
    identity, activeTenantId,
    scopeKey: `${identity.userId}:${activeTenantId}:${revision}`,
    canUpload: writable && identity.permissions.canUpload,
    canManage: writable && identity.permissions.canManage,
    canConfigure: identity.permissions.canConfigure,
    loginOpen, openLogin, closeLogin,
    selectTenant: (tenantId) => applyIdentity(identity, tenantId),
    sendCode: async (email) => (await sendEmailCode(email)).retryAfterSeconds,
    verifyCode: async (email, code) => {
      const next = await verifyEmailCode(email, code);
      ++refreshVersion.current;
      applyIdentity(next);
      setLoginOpen(false);
    },
    logout: async () => {
      const next = await logoutIdentity();
      ++refreshVersion.current;
      applyIdentity(next);
      setLoginOpen(false);
    },
  };

  return <AuthContext.Provider value={value}>
    {children}
    <LoginDialog />
  </AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}

import { useQueryClient } from "@tanstack/react-query";
import { createContext, type PropsWithChildren, useCallback, useContext, useEffect, useRef, useState } from "react";

import { ApiClientError, clearDataSource, configureAccountIdentity, configureDataSource, createGuestIdentity, errorMessage, getAuthIdentity, logoutIdentity, PRIMARY_CONNECTION, REMOTE_CONNECTION, sendEmailCode, verifyEmailCode, TENANT_ID } from "../../api/client";
import type { ApiConnection, AuthIdentity, DataSource } from "../../api/contracts";
import { useI18n } from "../../app/i18n";
import { LoginDialog } from "./LoginDialog";
import styles from "./auth.module.css";

interface AuthContextValue {
  identity: AuthIdentity;
  activeTenantId: string;
  activeSource: DataSource;
  sources: DataSource[];
  scopeKey: string;
  accountIdentity: AuthIdentity | null;
  accountConnection: ApiConnection | null;
  accountStatus: "unconfigured" | "checking" | "connected" | "unavailable";
  accountError: string | null;
  retryAccount: () => void;
  canUpload: boolean;
  canManage: boolean;
  canConfigure: boolean;
  loginOpen: boolean;
  openLogin: () => void;
  closeLogin: () => void;
  selectSource: (key: string) => void;
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
const defaultSource: DataSource = {
  key: `${PRIMARY_CONNECTION.id}:local`, kind: "local", connection: PRIMARY_CONNECTION,
  tenantId: TENANT_ID, identity: localIdentity,
};
const unavailable = async () => { throw new Error("账号服务尚未连接"); };
const AuthContext = createContext<AuthContextValue>({
  identity: localIdentity, activeTenantId: TENANT_ID, activeSource: defaultSource, sources: [defaultSource], scopeKey: "local",
  accountIdentity: null, accountConnection: null, accountStatus: "unconfigured", accountError: null,
  canUpload: true, canManage: true, canConfigure: true, loginOpen: false,
  retryAccount: () => undefined, openLogin: () => undefined, closeLogin: () => undefined,
  selectSource: () => undefined, selectTenant: () => undefined,
  sendCode: unavailable, verifyCode: unavailable, logout: unavailable,
});

function accountSources(connection: ApiConnection, identity: AuthIdentity): DataSource[] {
  if (identity.mode !== "cloud") throw new Error("该账号服务不支持远端登录");
  const needsPersonalTenant = identity.permissions.canUpload || identity.permissions.canManage || identity.permissions.canConfigure;
  if (!identity.publicTenantId || (!identity.isGuest && needsPersonalTenant && !identity.tenantId)) {
    throw new Error("当前身份缺少可访问的资料来源");
  }
  const sources: DataSource[] = [{ key: `${connection.id}:public`, kind: "public", connection, tenantId: identity.publicTenantId, identity }];
  if (!identity.isGuest && identity.tenantId && identity.tenantId !== identity.publicTenantId) {
    sources.push({ key: `${connection.id}:personal`, kind: "personal", connection, tenantId: identity.tenantId, identity });
  }
  return sources;
}

async function readIdentity(connection: ApiConnection, signal?: AbortSignal): Promise<AuthIdentity> {
  try { return await getAuthIdentity(signal, connection); }
  catch (failure) {
    if (!(failure instanceof ApiClientError) || failure.status !== 401) throw failure;
    signal?.throwIfAborted();
    return createGuestIdentity(signal, connection);
  }
}

export function AuthProvider({ children, remoteConnection = REMOTE_CONNECTION }: PropsWithChildren<{ remoteConnection?: ApiConnection | null }>) {
  const { t, locale } = useI18n();
  const queryClient = useQueryClient();
  const [primaryIdentity, setPrimaryIdentity] = useState<AuthIdentity | null>(null);
  const primary = useRef<AuthIdentity | null>(null);
  const [activeSource, setActiveSource] = useState<DataSource | null>(null);
  const current = useRef<DataSource | null>(null);
  const [accountIdentity, setAccountIdentity] = useState<AuthIdentity | null>(null);
  const [accountConnection, setAccountConnection] = useState<ApiConnection | null>(remoteConnection);
  const account = useRef<{ connection: ApiConnection | null; identity: AuthIdentity | null }>({ connection: remoteConnection, identity: null });
  const [accountStatus, setAccountStatus] = useState<AuthContextValue["accountStatus"]>(remoteConnection ? "checking" : "unconfigured");
  const [accountFailure, setAccountFailure] = useState<{ failure: unknown } | null>(null);
  const accountError = accountFailure ? errorMessage(accountFailure.failure, locale) : null;
  const [primaryFailure, setPrimaryFailure] = useState<{ failure: unknown } | null>(null);
  const primaryError = primaryFailure ? errorMessage(primaryFailure.failure, locale) : null;
  const [loginOpen, setLoginOpen] = useState(false);
  const [revision, setRevision] = useState(0);
  const accountVersion = useRef(0);
  const accountMutation = useRef(false);
  const refreshing = useRef<{ version: number; signal?: AbortSignal } | null>(null);
  const primaryVersion = useRef(0);
  const closeLogin = useCallback(() => setLoginOpen(false), []);

  const applySource = useCallback((next: DataSource | null) => {
    const previous = current.current;
    const changed = previous?.key !== next?.key || previous?.tenantId !== next?.tenantId
      || previous?.identity.userId !== next?.identity.userId || previous?.identity.csrfToken !== next?.identity.csrfToken
      || previous?.identity.permissions.canUpload !== next?.identity.permissions.canUpload
      || previous?.identity.permissions.canManage !== next?.identity.permissions.canManage
      || previous?.identity.permissions.canConfigure !== next?.identity.permissions.canConfigure;
    if (changed) {
      if (next) configureDataSource(next); else clearDataSource();
      void queryClient.cancelQueries();
      queryClient.clear();
      setRevision((value) => value + 1);
    } else if (next) configureDataSource(next);
    current.current = next;
    setActiveSource(next);
  }, [queryClient]);

  const applyAccount = useCallback((connection: ApiConnection, next: AuthIdentity | null) => {
    const available = next ? accountSources(connection, next) : [];
    if (account.current.connection?.id !== connection.id) ++accountVersion.current;
    configureAccountIdentity(connection, next);
    account.current = { connection, identity: next };
    setAccountConnection(connection);
    setAccountIdentity(next);
    if (current.current?.kind === "local") return;
    const selected = available.find((source) => source.key === current.current?.key)
      ?? (!current.current ? available.find((source) => source.kind === "personal") : undefined)
      ?? available[0];
    if (selected) applySource(selected);
    else if (primary.current?.mode === "local" && primary.current.tenantId) {
      applySource({ key: `${PRIMARY_CONNECTION.id}:local`, kind: "local", connection: PRIMARY_CONNECTION, tenantId: primary.current.tenantId, identity: primary.current });
    } else applySource(null);
  }, [applySource]);

  const refreshAccount = useCallback(async (signal?: AbortSignal) => {
    const connection = account.current.connection;
    if (!connection || accountMutation.current
      || (refreshing.current?.version === accountVersion.current && !refreshing.current.signal?.aborted)) return;
    const version = ++accountVersion.current;
    refreshing.current = { version, signal };
    setAccountStatus("checking");
    let expired = false;
    try {
      let next: AuthIdentity;
      try { next = await getAuthIdentity(signal, connection); }
      catch (failure) {
        if (!(failure instanceof ApiClientError) || failure.status !== 401) throw failure;
        if (signal?.aborted || version !== accountVersion.current || accountMutation.current) return;
        expired = true;
        next = await createGuestIdentity(signal, connection);
      }
      if (signal?.aborted || version !== accountVersion.current) return;
      applyAccount(connection, next);
      setAccountStatus("connected");
      setAccountFailure(null);
    } catch (failure) {
      if (!signal?.aborted && version === accountVersion.current) {
        if (expired) applyAccount(connection, null);
        setAccountStatus("unavailable");
        setAccountFailure({ failure });
      }
    } finally {
      if (refreshing.current?.version === version) refreshing.current = null;
    }
  }, [applyAccount]);

  const connectPrimary = useCallback(async (signal?: AbortSignal) => {
    const version = ++primaryVersion.current;
    try {
      const next = await readIdentity(PRIMARY_CONNECTION, signal);
      if (signal?.aborted || version !== primaryVersion.current) return;
      if (next.mode === "local") {
        if (!next.tenantId) throw new Error("本机服务缺少资料空间");
        primary.current = next;
        setPrimaryIdentity(next);
        if (!current.current || current.current.kind === "local") {
          applySource({ key: `${PRIMARY_CONNECTION.id}:local`, kind: "local", connection: PRIMARY_CONNECTION, tenantId: next.tenantId, identity: next });
        }
        if (account.current.connection) void refreshAccount(signal);
      } else {
        applyAccount(PRIMARY_CONNECTION, next);
        primary.current = next;
        setPrimaryIdentity(next);
        setAccountStatus("connected");
        setAccountFailure(null);
      }
      setPrimaryFailure(null);
    } catch (failure) {
      if (!signal?.aborted && version === primaryVersion.current) {
        setPrimaryFailure({ failure });
        if (account.current.connection) void refreshAccount(signal);
      }
    }
  }, [applySource, applyAccount, refreshAccount]);

  useEffect(() => {
    const controller = new AbortController();
    void connectPrimary(controller.signal);
    const onFocus = () => {
      if (!primary.current) void connectPrimary(controller.signal);
      else void refreshAccount(controller.signal);
    };
    const onAuthRequired = (event: Event) => {
      const connectionId = (event as CustomEvent<{ connectionId: string }>).detail?.connectionId;
      if (!connectionId || connectionId === account.current.connection?.id) void refreshAccount(controller.signal);
    };
    window.addEventListener("focus", onFocus);
    window.addEventListener("seekino-auth-required", onAuthRequired);
    return () => {
      controller.abort();
      ++primaryVersion.current;
      ++accountVersion.current;
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("seekino-auth-required", onAuthRequired);
    };
  }, [connectPrimary, refreshAccount]);

  if (!activeSource) {
    const error = primaryError ?? accountError;
    return <div className={styles.loading} role={error ? "alert" : "status"}>
      <p>{error ? t("暂时无法连接资料服务：{error}", { error }) : t("正在连接服务…")}</p>
      {error ? <button className="button-primary" type="button" onClick={() => void connectPrimary()}>{t("重试连接")}</button> : null}
    </div>;
  }

  const sources: DataSource[] = [];
  if (primaryIdentity?.mode === "local" && primaryIdentity.tenantId) {
    sources.push({ key: `${PRIMARY_CONNECTION.id}:local`, kind: "local", connection: PRIMARY_CONNECTION, tenantId: primaryIdentity.tenantId, identity: primaryIdentity });
  }
  if (accountIdentity && accountConnection) sources.push(...accountSources(accountConnection, accountIdentity));
  const identity = activeSource.identity;
  const writable = activeSource.kind !== "public" || (!identity.isGuest && identity.permissions.canConfigure);
  const value: AuthContextValue = {
    identity, activeTenantId: activeSource.tenantId, activeSource, sources,
    scopeKey: `${activeSource.key}:${identity.userId}:${activeSource.tenantId}:${revision}`,
    accountIdentity, accountConnection, accountStatus, accountError,
    retryAccount: () => { void refreshAccount(); },
    canUpload: writable && identity.permissions.canUpload,
    canManage: writable && identity.permissions.canManage,
    canConfigure: identity.permissions.canConfigure,
    loginOpen, openLogin: () => { if (account.current.identity && !accountMutation.current) setLoginOpen(true); }, closeLogin,
    selectSource: (key) => { const next = sources.find((source) => source.key === key); if (next) applySource(next); },
    selectTenant: (tenantId) => { const next = sources.find((source) => source.connection.id === activeSource.connection.id && source.tenantId === tenantId); if (next) applySource(next); },
    sendCode: async (email) => {
      if (!account.current.connection || !account.current.identity) return unavailable();
      return (await sendEmailCode(email, account.current.connection)).retryAfterSeconds;
    },
    verifyCode: async (email, code) => {
      const connection = account.current.connection;
      if (!connection) return unavailable();
      if (accountMutation.current) throw new Error("账号操作正在进行，请稍后重试");
      const version = ++accountVersion.current;
      accountMutation.current = true;
      setAccountStatus(account.current.identity ? "connected" : "unavailable");
      try {
        const next = await verifyEmailCode(email, code, connection);
        if (version !== accountVersion.current) return;
        applyAccount(connection, next);
        setAccountStatus("connected"); setAccountFailure(null); setLoginOpen(false);
      } finally { accountMutation.current = false; }
    },
    logout: async () => {
      const connection = account.current.connection;
      if (!connection) return unavailable();
      if (accountMutation.current) throw new Error("账号操作正在进行，请稍后重试");
      const version = ++accountVersion.current;
      accountMutation.current = true;
      setAccountStatus(account.current.identity ? "connected" : "unavailable");
      try {
        const next = await logoutIdentity(connection);
        if (version !== accountVersion.current) return;
        applyAccount(connection, next);
        setAccountStatus("connected"); setAccountFailure(null); setLoginOpen(false);
      } finally { accountMutation.current = false; }
    },
  };

  return <AuthContext.Provider value={value}>{children}<LoginDialog /></AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}

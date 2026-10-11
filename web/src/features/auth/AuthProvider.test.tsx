import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiConnection, AuthIdentity } from "../../api/contracts";
import { AuthProvider, useAuth } from "./AuthProvider";

const api = vi.hoisted(() => ({
  get: vi.fn(), guest: vi.fn(), configure: vi.fn(), configureAccount: vi.fn(), clear: vi.fn(), send: vi.fn(), verify: vi.fn(), logout: vi.fn(),
}));
vi.mock("../../api/client", async (original) => ({
  ...await original<typeof import("../../api/client")>(),
  getAuthIdentity: api.get, createGuestIdentity: api.guest, configureDataSource: api.configure, configureAccountIdentity: api.configureAccount, clearDataSource: api.clear,
  sendEmailCode: api.send, verifyEmailCode: api.verify, logoutIdentity: api.logout,
}));

const guest: AuthIdentity = {
  userId: "guest-a", email: null, isGuest: true, tenantId: null,
  publicTenantId: "public-courses", mode: "cloud", csrfToken: "guest-token",
  permissions: { canUpload: false, canManage: false, canConfigure: false },
};
const member: AuthIdentity = {
  ...guest, userId: "user-a", email: "learner@example.com", isGuest: false,
  tenantId: "personal-a", csrfToken: "member-token",
  permissions: { canUpload: true, canManage: true, canConfigure: false },
};

function Probe() {
  const auth = useAuth();
  return <>
    <p>{auth.identity.userId}:{auth.activeTenantId}</p>
    <output data-testid="scope">{auth.scopeKey}</output>
    <p>账号：{auth.accountIdentity?.userId ?? "未连接"}</p>
    <p>连接：{auth.accountStatus}</p>
    {auth.accountError ? <p role="alert">{auth.accountError}</p> : null}
    {auth.sources.map((source) => <button key={source.key} onClick={() => auth.selectSource(source.key)}>来源：{source.kind}</button>)}
    <p>{auth.canUpload ? "可以上传" : "只读体验"}</p>
    <button onClick={auth.openLogin}>邮箱登录</button>
    <button onClick={() => auth.selectTenant(auth.identity.publicTenantId)}>公开课程</button>
    <button onClick={() => { if (auth.identity.tenantId) auth.selectTenant(auth.identity.tenantId); }}>个人资料</button>
    <button onClick={() => void auth.logout()}>退出登录</button>
    <button onClick={auth.retryAccount}>重连账号</button>
  </>;
}

function mount(remoteConnection: ApiConnection | null = null) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["private-results"], "sensitive");
  render(<QueryClientProvider client={queryClient}><AuthProvider remoteConnection={remoteConnection}><Probe /></AuthProvider></QueryClientProvider>);
  return queryClient;
}

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue(guest);
  api.guest.mockResolvedValue(guest);
  api.send.mockResolvedValue({ retryAfterSeconds: 60 });
  api.verify.mockResolvedValue(member);
  api.logout.mockResolvedValue({ ...guest, userId: "guest-b" });
});
afterEach(() => cleanup());

describe("cloud identity", () => {
  it("waits for the identity before rendering protected data and clears previous cached results", async () => {
    let finish!: (identity: AuthIdentity) => void;
    api.get.mockReturnValue(new Promise<AuthIdentity>((resolve) => { finish = resolve; }));
    const queryClient = mount();
    expect(screen.queryByText("只读体验")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("正在连接服务");
    await act(async () => finish(guest));
    expect(await screen.findByText("guest-a:public-courses")).toBeInTheDocument();
    expect(queryClient.getQueryData(["private-results"])).toBeUndefined();
    expect(api.configure).toHaveBeenCalledWith(expect.objectContaining({ identity: guest, tenantId: "public-courses", kind: "public" }));
  });

  it("creates a guest only when me returns an expired or missing session", async () => {
    const { ApiClientError } = await import("../../api/client");
    api.get.mockRejectedValue(new ApiClientError(401, null));
    mount();
    expect(await screen.findByText("只读体验")).toBeInTheDocument();
    expect(api.guest).toHaveBeenCalledTimes(1);
  });

  it("shows a connection error instead of granting local permissions on a failed cloud bootstrap", async () => {
    api.get.mockRejectedValue(new Error("网络不可用"));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("网络不可用");
    expect(screen.queryByText("可以上传")).not.toBeInTheDocument();
    expect(api.guest).not.toHaveBeenCalled();
  });

  it("rejects a signed-in identity without a personal tenant", async () => {
    api.get.mockResolvedValue({ ...member, tenantId: null });
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("当前身份缺少可访问的资料来源");
    expect(api.configure).not.toHaveBeenCalled();
    expect(screen.queryByText("可以上传")).not.toBeInTheDocument();
  });

  it("switches a signed-in user between personal writes and public read-only data, then logs out to a new guest", async () => {
    api.get.mockResolvedValue(member);
    const queryClient = mount();
    const user = userEvent.setup();
    expect(await screen.findByText("可以上传")).toBeInTheDocument();
    queryClient.setQueryData(["private-results"], "private answer");
    await user.click(screen.getByText("公开课程"));
    expect(screen.getByText("user-a:public-courses")).toBeInTheDocument();
    expect(screen.getByText("只读体验")).toBeInTheDocument();
    expect(queryClient.getQueryData(["private-results"])).toBeUndefined();
    await user.click(screen.getByText("个人资料"));
    expect(screen.getByText("可以上传")).toBeInTheDocument();
    await user.click(screen.getByText("退出登录"));
    expect(await screen.findByText("guest-b:public-courses")).toBeInTheDocument();
    expect(screen.getByText("只读体验")).toBeInTheDocument();
  });

  it("sends a normalized email, enforces the resend cooldown, and preserves the public source after verification", async () => {
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "邮箱登录" }));
    await user.type(screen.getByLabelText("邮箱"), "Learner@Example.COM");
    await user.click(screen.getByRole("button", { name: "发送验证码" }));
    expect(api.send).toHaveBeenCalledWith("learner@example.com", expect.objectContaining({ id: expect.any(String) }));
    expect(await screen.findByText("验证码已发送至 learner@example.com")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /秒后重新发送/ })).toBeDisabled();
    await user.type(screen.getByLabelText("验证码"), "123456");
    await user.click(screen.getByRole("button", { name: /^登录$/ }));
    await waitFor(() => expect(api.verify).toHaveBeenCalledWith("learner@example.com", "123456", expect.objectContaining({ id: expect.any(String) })));
    expect(await screen.findByText("user-a:public-courses")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps the guest identity when verification fails and prevents using a code after changing email", async () => {
    api.verify.mockRejectedValue(new Error("验证码不正确或已过期"));
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "邮箱登录" }));
    await user.type(screen.getByLabelText("邮箱"), "learner@example.com");
    await user.click(screen.getByRole("button", { name: "发送验证码" }));
    await screen.findByText("验证码已发送至 learner@example.com");
    await user.type(screen.getByLabelText("验证码"), "123456");
    await user.click(screen.getByRole("button", { name: /^登录$/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("验证码不正确或已过期");
    expect(screen.getByText("guest-a:public-courses")).toBeInTheDocument();
    await user.type(screen.getByLabelText("邮箱"), ".changed");
    expect(screen.getByRole("button", { name: /^登录$/ })).toBeDisabled();
  });

  it("updates an elapsed resend cooldown when reopening the login dialog", async () => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(10_000);
    try {
      mount();
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "邮箱登录" }));
      await user.type(screen.getByLabelText("邮箱"), "learner@example.com");
      await user.click(screen.getByRole("button", { name: "发送验证码" }));
      expect(await screen.findByRole("button", { name: "60 秒后重新发送" })).toBeDisabled();
      await user.click(screen.getByRole("button", { name: "关闭登录" }));
      clock.mockReturnValue(71_000);
      await user.click(screen.getByRole("button", { name: "邮箱登录" }));
      expect(await screen.findByRole("button", { name: "重新发送验证码" })).toBeEnabled();
    } finally { clock.mockRestore(); }
  });
});


const local: AuthIdentity = {
  userId: "local-user", email: null, isGuest: false, tenantId: "local-default", publicTenantId: "public-courses",
  mode: "local", csrfToken: "", permissions: { canUpload: true, canManage: true, canConfigure: true },
};
const remote: ApiConnection = { id: "remote:test", baseUrl: "/remote" };

async function logIn() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "邮箱登录" }));
  await user.type(screen.getByLabelText("邮箱"), "learner@example.com");
  await user.click(screen.getByRole("button", { name: "发送验证码" }));
  await screen.findByText("验证码已发送至 learner@example.com");
  await user.type(screen.getByLabelText("验证码"), "123456");
  await user.click(screen.getByRole("button", { name: /^登录$/ }));
}

describe("local data with a remote account", () => {
  beforeEach(() => {
    api.get.mockImplementation((_signal: AbortSignal | undefined, connection: ApiConnection) => Promise.resolve(connection.id === remote.id ? guest : local));
  });

  it("keeps local capabilities when no account server is configured", async () => {
    mount();
    expect(await screen.findByText("local-user:local-default")).toBeInTheDocument();
    expect(screen.getByText("可以上传")).toBeInTheDocument();
    expect(screen.getByText("连接：unconfigured")).toBeInTheDocument();
    expect(screen.queryByText("来源：public")).not.toBeInTheDocument();
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it("renders local data while the remote identity request is still pending", async () => {
    api.get.mockImplementation((_signal: AbortSignal | undefined, connection: ApiConnection) => connection.id === remote.id ? new Promise(() => undefined) : Promise.resolve(local));
    mount(remote);
    expect(await screen.findByText("local-user:local-default")).toBeInTheDocument();
    expect(screen.getByText("可以上传")).toBeInTheDocument();
    expect(screen.getByText("连接：checking")).toBeInTheDocument();
  });

  it("keeps local scope and cached work through remote login and logout", async () => {
    const queryClient = mount(remote);
    await screen.findByText("账号：guest-a");
    const scope = screen.getByTestId("scope").textContent;
    queryClient.setQueryData(["local-work"], "upload and playback");
    api.configure.mockClear();
    await logIn();
    expect(await screen.findByText("账号：user-a")).toBeInTheDocument();
    expect(screen.getByText("local-user:local-default")).toBeInTheDocument();
    expect(screen.getByTestId("scope")).toHaveTextContent(scope!);
    expect(queryClient.getQueryData(["local-work"])).toBe("upload and playback");
    expect(api.configure).not.toHaveBeenCalled();
    expect(screen.getByText("来源：personal")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByText("退出登录"));
    expect(await screen.findByText("账号：guest-b")).toBeInTheDocument();
    expect(screen.getByTestId("scope")).toHaveTextContent(scope!);
    expect(screen.getByText("可以上传")).toBeInTheDocument();
    expect(queryClient.getQueryData(["local-work"])).toBe("upload and playback");
    expect(api.configure).not.toHaveBeenCalled();
  });

  it("isolates public and personal data, then removes private scope on logout", async () => {
    api.get.mockImplementation((_signal: AbortSignal | undefined, connection: ApiConnection) => Promise.resolve(connection.id === remote.id ? member : local));
    const queryClient = mount(remote);
    await screen.findByText("账号：user-a");
    const user = userEvent.setup();
    await user.click(screen.getByText("来源：public"));
    expect(screen.getByText("user-a:public-courses")).toBeInTheDocument();
    expect(screen.getByText("只读体验")).toBeInTheDocument();
    await user.click(screen.getByText("来源：personal"));
    expect(screen.getByText("user-a:personal-a")).toBeInTheDocument();
    expect(screen.getByText("可以上传")).toBeInTheDocument();
    queryClient.setQueryData(["private-work"], "private answer");
    await user.click(screen.getByText("退出登录"));
    expect(await screen.findByText("guest-b:public-courses")).toBeInTheDocument();
    expect(queryClient.getQueryData(["private-work"])).toBeUndefined();
    expect(screen.queryByText("来源：personal")).not.toBeInTheDocument();
    await user.click(screen.getByText("来源：local"));
    expect(screen.getByText("local-user:local-default")).toBeInTheDocument();
    expect(screen.getByText("可以上传")).toBeInTheDocument();
  });

  it("keeps local data available when the remote service is unreachable", async () => {
    api.get.mockImplementation((_signal: AbortSignal | undefined, connection: ApiConnection) => connection.id === remote.id ? Promise.reject(new Error("远端不可用")) : Promise.resolve(local));
    mount(remote);
    expect(await screen.findByText("远端不可用")).toBeInTheDocument();
    expect(screen.getByText("local-user:local-default")).toBeInTheDocument();
    expect(screen.getByText("可以上传")).toBeInTheDocument();
    expect(screen.getByText("连接：unavailable")).toBeInTheDocument();
    expect(api.guest).not.toHaveBeenCalled();
  });

  it("does not promote a local server response into a remote account", async () => {
    api.get.mockResolvedValue(local);
    mount(remote);
    expect(await screen.findByText("该账号服务不支持远端登录")).toBeInTheDocument();
    expect(screen.getByText("账号：未连接")).toBeInTheDocument();
    expect(screen.getByText("可以上传")).toBeInTheDocument();
  });

  it("ignores a local authorization event while preserving the remote session", async () => {
    mount(remote);
    await screen.findByText("账号：guest-a");
    const calls = api.get.mock.calls.length;
    await act(async () => window.dispatchEvent(new CustomEvent("anivag-auth-required", { detail: { connectionId: "other-local" } })));
    expect(api.get).toHaveBeenCalledTimes(calls);
  });

  it("handles remote session expiry without changing the local scope", async () => {
    api.get.mockImplementation((_signal: AbortSignal | undefined, connection: ApiConnection) => Promise.resolve(connection.id === remote.id ? member : local));
    const queryClient = mount(remote);
    await screen.findByText("账号：user-a");
    const scope = screen.getByTestId("scope").textContent;
    queryClient.setQueryData(["local-work"], "keep");
    const { ApiClientError } = await import("../../api/client");
    api.get.mockRejectedValue(new ApiClientError(401, null));
    await act(async () => window.dispatchEvent(new CustomEvent("anivag-auth-required", { detail: { connectionId: remote.id } })));
    expect(await screen.findByText("账号：guest-a")).toBeInTheDocument();
    expect(screen.getByTestId("scope")).toHaveTextContent(scope!);
    expect(queryClient.getQueryData(["local-work"])).toBe("keep");
  });

  it("removes expired remote private data if guest bootstrap also fails", async () => {
    api.get.mockImplementation((_signal: AbortSignal | undefined, connection: ApiConnection) => Promise.resolve(connection.id === remote.id ? member : local));
    const queryClient = mount(remote);
    await screen.findByText("账号：user-a");
    await userEvent.setup().click(screen.getByText("来源：personal"));
    queryClient.setQueryData(["private-work"], "secret");
    const { ApiClientError } = await import("../../api/client");
    api.get.mockRejectedValue(new ApiClientError(401, null));
    api.guest.mockRejectedValue(new Error("游客连接失败"));
    await act(async () => window.dispatchEvent(new CustomEvent("anivag-auth-required", { detail: { connectionId: remote.id } })));
    expect(await screen.findByText("local-user:local-default")).toBeInTheDocument();
    expect(screen.getByText("账号：未连接")).toBeInTheDocument();
    expect(queryClient.getQueryData(["private-work"])).toBeUndefined();
  });

  it("ignores a pending code delivery after focus switches to another guest", async () => {
    let finish!: (result: { retryAfterSeconds: number }) => void;
    api.send.mockReturnValue(new Promise<{ retryAfterSeconds: number }>((resolve) => { finish = resolve; }));
    mount(remote);
    await screen.findByText("账号：guest-a");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "邮箱登录" }));
    await user.type(screen.getByLabelText("邮箱"), "learner@example.com");
    await user.click(screen.getByRole("button", { name: "发送验证码" }));
    expect(screen.getByRole("button", { name: "发送验证码" })).toBeDisabled();

    api.get.mockResolvedValue({ ...guest, userId: "guest-b", csrfToken: "guest-b-token" });
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(await screen.findByText("账号：guest-b")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送验证码" })).toBeEnabled();
    await act(async () => finish({ retryAfterSeconds: 60 }));
    expect(screen.queryByText("验证码已发送至 learner@example.com")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送验证码" })).toBeEnabled();
    await user.type(screen.getByLabelText("验证码"), "123456");
    expect(screen.getByRole("button", { name: /^登录$/ })).toBeDisabled();
    expect(api.verify).not.toHaveBeenCalled();
  });

  it("does not let focus refresh overwrite a pending login", async () => {
    let finish!: (identity: AuthIdentity) => void;
    api.verify.mockReturnValue(new Promise<AuthIdentity>((resolve) => { finish = resolve; }));
    mount(remote);
    await screen.findByText("账号：guest-a");
    await logIn();
    const calls = api.get.mock.calls.length;
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(api.get).toHaveBeenCalledTimes(calls);
    await act(async () => finish(member));
    expect(await screen.findByText("账号：user-a")).toBeInTheDocument();
    expect(screen.getByText("local-user:local-default")).toBeInTheDocument();
  });

  it("does not let focus refresh overwrite a pending logout", async () => {
    api.get.mockImplementation((_signal: AbortSignal | undefined, connection: ApiConnection) => Promise.resolve(connection.id === remote.id ? member : local));
    let finish!: (identity: AuthIdentity) => void;
    api.logout.mockReturnValue(new Promise<AuthIdentity>((resolve) => { finish = resolve; }));
    mount(remote);
    await screen.findByText("账号：user-a");
    await userEvent.setup().click(screen.getByText("退出登录"));
    const calls = api.get.mock.calls.length;
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(api.get).toHaveBeenCalledTimes(calls);
    await act(async () => finish(guest));
    expect(await screen.findByText("账号：guest-a")).toBeInTheDocument();
    expect(screen.getByText("local-user:local-default")).toBeInTheDocument();
  });

  it("does not bootstrap a stale guest after a successful login", async () => {
    mount(remote);
    await screen.findByText("账号：guest-a");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "邮箱登录" }));
    await user.type(screen.getByLabelText("邮箱"), "learner@example.com");
    await user.click(screen.getByRole("button", { name: "发送验证码" }));
    await screen.findByText("验证码已发送至 learner@example.com");
    await user.type(screen.getByLabelText("验证码"), "123456");
    let rejectMe!: (error: Error) => void;
    api.get.mockReturnValue(new Promise<AuthIdentity>((_resolve, reject) => { rejectMe = reject; }));
    await act(async () => window.dispatchEvent(new Event("focus")));
    await user.click(screen.getByRole("button", { name: /^登录$/ }));
    expect(await screen.findByText("账号：user-a")).toBeInTheDocument();
    const { ApiClientError } = await import("../../api/client");
    await act(async () => rejectMe(new ApiClientError(401, null)));
    expect(api.guest).not.toHaveBeenCalled();
    expect(screen.getByText("账号：user-a")).toBeInTheDocument();
  });

  it("invalidates selected private work when the server withdraws permissions", async () => {
    api.get.mockImplementation((_signal: AbortSignal | undefined, connection: ApiConnection) => Promise.resolve(connection.id === remote.id ? member : local));
    const queryClient = mount(remote);
    await screen.findByText("账号：user-a");
    await userEvent.setup().click(screen.getByText("来源：personal"));
    const scope = screen.getByTestId("scope").textContent;
    queryClient.setQueryData(["private-work"], "pending work");
    api.get.mockResolvedValue({ ...member, permissions: { canUpload: false, canManage: false, canConfigure: false } });
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(await screen.findByText("只读体验")).toBeInTheDocument();
    expect(screen.getByTestId("scope").textContent).not.toBe(scope);
    expect(queryClient.getQueryData(["private-work"])).toBeUndefined();
  });

  it("ignores a retry response that arrives after the provider unmounts", async () => {
    api.get.mockImplementation((_signal: AbortSignal | undefined, connection: ApiConnection) => connection.id === remote.id ? Promise.reject(new Error("远端不可用")) : Promise.resolve(local));
    mount(remote);
    await screen.findByText("连接：unavailable");
    let finish!: (identity: AuthIdentity) => void;
    api.get.mockReturnValue(new Promise<AuthIdentity>((resolve) => { finish = resolve; }));
    await userEvent.setup().click(screen.getByText("重连账号"));
    const calls = api.configureAccount.mock.calls.length;
    cleanup();
    await act(async () => finish(member));
    expect(api.configureAccount).toHaveBeenCalledTimes(calls);
  });

  it("uses remote public data without inventing local capabilities when the primary server is unavailable", async () => {
    api.get.mockImplementation((_signal: AbortSignal | undefined, connection: ApiConnection) => connection.id === remote.id ? Promise.resolve(guest) : Promise.reject(new Error("本机不可用")));
    mount(remote);
    expect(await screen.findByText("guest-a:public-courses")).toBeInTheDocument();
    expect(screen.getByText("只读体验")).toBeInTheDocument();
    expect(screen.queryByText("来源：local")).not.toBeInTheDocument();
  });
});

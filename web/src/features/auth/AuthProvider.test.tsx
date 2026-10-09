import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuthIdentity } from "../../api/contracts";
import { AuthProvider, useAuth } from "./AuthProvider";

const api = vi.hoisted(() => ({
  get: vi.fn(), guest: vi.fn(), configure: vi.fn(), send: vi.fn(), verify: vi.fn(), logout: vi.fn(),
}));
vi.mock("../../api/client", async (original) => ({
  ...await original<typeof import("../../api/client")>(),
  getAuthIdentity: api.get, createGuestIdentity: api.guest, configureApiIdentity: api.configure,
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
    <p>{auth.canUpload ? "可以上传" : "只读体验"}</p>
    <button onClick={auth.openLogin}>邮箱登录</button>
    <button onClick={() => auth.selectTenant(auth.identity.publicTenantId)}>公开课程</button>
    <button onClick={() => { if (auth.identity.tenantId) auth.selectTenant(auth.identity.tenantId); }}>个人资料</button>
    <button onClick={() => void auth.logout()}>退出登录</button>
  </>;
}

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["private-results"], "sensitive");
  render(<QueryClientProvider client={queryClient}><AuthProvider><Probe /></AuthProvider></QueryClientProvider>);
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
    expect(api.configure).toHaveBeenCalledWith(guest, "public-courses");
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

  it("sends a normalized email, enforces the resend cooldown, and opens the personal tenant after verification", async () => {
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "邮箱登录" }));
    await user.type(screen.getByLabelText("邮箱"), "Learner@Example.COM");
    await user.click(screen.getByRole("button", { name: "发送验证码" }));
    expect(api.send).toHaveBeenCalledWith("learner@example.com");
    expect(await screen.findByText("验证码已发送至 learner@example.com")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /秒后重新发送/ })).toBeDisabled();
    await user.type(screen.getByLabelText("验证码"), "123456");
    await user.click(screen.getByRole("button", { name: /^登录$/ }));
    await waitFor(() => expect(api.verify).toHaveBeenCalledWith("learner@example.com", "123456"));
    expect(await screen.findByText("user-a:personal-a")).toBeInTheDocument();
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
});

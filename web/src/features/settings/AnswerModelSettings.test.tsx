import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../../api/client";
import type { PersonalModelConnection, PersonalModelSettings } from "../../api/contracts";
import { configuredModels } from "../../test/personalModelFixtures";
import { AnswerModelSettings } from "./AnswerModelSettings";
import { PersonalConnectionForm } from "./PersonalConnectionForm";
import { preferredChatModel, rememberChatModel, savedChatModelId } from "./usePersonalChatModels";

vi.mock("../../app/workspace", () => ({ useWorkspace: () => ({ mode: "live" }) }));
vi.mock("../auth/AuthProvider", () => ({ useAuth: () => ({ scopeKey: "local", canConfigure: true }) }));

const yamlModel = { id: "yaml:test-primary", modelId: "my/team-model", isYamlDefault: true, aliases: ["default"] };
const yamlConnection: PersonalModelConnection = {
  ...configuredModels.connections[0]!, origin: "yaml", connectionId: "yaml-source", name: "home_proxy", models: [yamlModel], revision: 0,
};

beforeEach(() => { localStorage.clear(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("shows configured YAML connections as editable with a credential mask instead of an origin badge", async () => {
  vi.spyOn(api, "getPersonalModelSettings").mockResolvedValue({ defaultModelId: "default", connections: [yamlConnection] });
  render(<AnswerModelSettings active />);
  const card = await screen.findByRole("article");
  expect(within(card).getByRole("heading", { name: "home_proxy" })).toBeVisible();
  expect(within(card).queryByText("来自 YAML")).not.toBeInTheDocument();
  expect(within(card).getByLabelText("API Key 已配置")).toHaveTextContent("••••••••");
  expect(within(card).getByText("my/team-model")).toBeVisible();
  expect(within(card).getByRole("button", { name: "测试连接" })).toBeEnabled();
  expect(within(card).getByRole("button", { name: "编辑" })).toBeEnabled();
  expect(within(card).getByRole("button", { name: "删除" })).toBeEnabled();
  expect(screen.queryByRole("textbox", { name: "API Key" })).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "新对话默认模型" })).not.toBeInTheDocument();
});

it("does not repeat shared model IDs when opening the connection form", () => {
  const shared = { ...yamlConnection, models: [yamlModel, { ...yamlModel }] };
  render(<PersonalConnectionForm id={shared.connectionId} connection={shared} busy={false} save={() => undefined} />);
  expect(screen.getByLabelText("模型 ID")).toHaveValue(yamlModel.modelId);
});

it("displays a saved configured key as a mask and omits it from an unchanged edit", async () => {
  vi.spyOn(api, "getPersonalModelSettings").mockResolvedValue(configuredModels);
  const save = vi.spyOn(api, "savePersonalModelConnection").mockResolvedValue(configuredModels);
  render(<AnswerModelSettings active />);
  await userEvent.click(await screen.findByRole("button", { name: "编辑" }));
  const key = screen.getByLabelText("API Key");
  expect(key).toHaveAttribute("type", "password");
  expect(key).toHaveAttribute("placeholder", "••••••••");
  expect(key).toHaveValue("");
  await userEvent.click(screen.getByRole("button", { name: "保存连接" }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save.mock.calls[0]![1]).not.toHaveProperty("apiKey");
});

it("selects the YAML default on first use, then honors a valid recent choice or an explicit default", () => {
  const settings: PersonalModelSettings = { defaultModelId: "default", connections: [yamlConnection, ...configuredModels.connections] };
  expect(preferredChatModel(settings)).toBe(yamlModel.id);
  expect(savedChatModelId(settings.connections, "default")).toBe(yamlModel.id);
  const personal = configuredModels.connections[0]!.models[0]!;
  rememberChatModel(personal.id);
  expect(preferredChatModel(settings)).toBe(personal.id);
  expect(preferredChatModel({ ...settings, defaultModelId: yamlModel.id })).toBe(yamlModel.id);
  expect(preferredChatModel({ ...settings, connections: [yamlConnection] })).toBe(yamlModel.id);
  rememberChatModel(yamlModel.id);
  expect(preferredChatModel({ ...settings, connections: [] })).toBe("default");
});

it("offers compatible registrations per role and saves the selected service with its revision", async () => {
  const visual = { id: "visual:batch", modelId: "same-name", roles: ["vision", "ocr"] as ("vision"|"ocr")[] };
  const audio = { id: "audio:only", modelId: "speech-only", roles: ["transcription"] as "transcription"[] };
  const before: PersonalModelSettings = {
    defaultModelId: "default", revision: 41, connections: [{ ...yamlConnection, models: [visual, audio] },
      { ...yamlConnection, connectionId: "studio", name: "Studio", models: [{ ...visual, id: "visual:studio" }] }],
    roles: [{ role: "vision", modelId: null, overridden: false, activation: "restart" },
      { role: "transcription", modelId: audio.id, overridden: false, activation: "restart" }],
  };
  vi.spyOn(api, "getPersonalModelSettings").mockResolvedValue(before);
  const set = vi.spyOn(api, "saveModelRole").mockResolvedValue({ ...before, revision: 42,
    roles: [{ role: "vision", modelId: visual.id, overridden: true, activation: "restart" }] });
  render(<AnswerModelSettings active section="roles" />);
  const picker = await screen.findByRole("combobox", { name: "画面理解" });
  expect(within(picker).queryByText(/speech-only/)).toBeNull();
  expect(within(picker).getAllByRole("option", { name: visual.modelId })).toHaveLength(2);
  expect(screen.getByRole("combobox", { name: "语音转写" })).toHaveAccessibleDescription("原配置 · home_proxy");
  await userEvent.selectOptions(picker, visual.id);
  expect(set).toHaveBeenCalledWith("vision", visual.id, 41, expect.any(AbortSignal));
  await waitFor(() => expect(picker).toHaveValue(visual.id));
  expect(picker).toHaveAccessibleDescription("home_proxy");
  await userEvent.selectOptions(picker, "");
  expect(set).toHaveBeenLastCalledWith("vision", null, 42, expect.any(AbortSignal));
});

it("discovers model IDs into a draft and saves per-model types only after confirmation", async () => {
  const save = vi.fn();
  const fetchIds = vi.spyOn(api, "discoverModelIds").mockResolvedValue({ modelIds: ["my-vector"] });
  render(<PersonalConnectionForm id="draft" busy={false} save={save} />);
  await userEvent.type(screen.getByLabelText("服务名称"), "我的连接");
  await userEvent.click(screen.getByRole("button", { name: "获取模型列表" }));
  expect(fetchIds).toHaveBeenCalledWith("draft", expect.not.objectContaining({ apiKey: expect.anything() }), expect.any(AbortSignal));
  expect(await screen.findByText("获取到 1 个模型，0 个已注册")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "添加 my-vector" }));
  expect(screen.getByText("获取到 1 个模型，1 个已注册")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "添加 my-vector" })).toBeNull();
  await userEvent.selectOptions(screen.getByLabelText("模型类型"), "embedding");
  await userEvent.type(screen.getByLabelText("向量维度"), "768");
  expect(save).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "保存连接" }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ modelIds: ["my-vector"],
    models: [{ modelId: "my-vector", kind: "embedding", dimensions: 768 }] }));
});

it("shows fetched models inline, marks registered ones and adds the rest without duplicates", async () => {
  vi.spyOn(api, "discoverModelIds").mockResolvedValue({ modelIds: ["kept", "new-a", "new-b"] });
  render(<PersonalConnectionForm id="draft" busy={false} save={vi.fn()} />);
  await userEvent.type(screen.getByLabelText("服务名称"), "我的连接");
  await userEvent.type(screen.getByLabelText("模型 ID"), "kept");
  await userEvent.click(screen.getByRole("button", { name: "获取模型列表" }));
  expect(await screen.findByText("获取到 3 个模型，1 个已注册")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "添加 kept" })).toBeNull();
  expect(screen.getByTitle("kept 已注册")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "全部添加（2）" }));
  expect(screen.getByText("获取到 3 个模型，3 个已注册")).toBeInTheDocument();
  expect(screen.getAllByRole("textbox", { name: /^模型 ID/ }).map((input) => (input as HTMLInputElement).value)).toEqual(["kept", "new-a", "new-b"]);
  expect(screen.queryByRole("button", { name: /^全部添加/ })).toBeNull();
});

it("explains services without a model list instead of reporting an error", async () => {
  vi.spyOn(api, "discoverModelIds").mockResolvedValue({ modelIds: [], listAvailable: false });
  render(<PersonalConnectionForm id="draft" busy={false} save={vi.fn()} />);
  await userEvent.type(screen.getByLabelText("服务名称"), "我的连接");
  await userEvent.click(screen.getByRole("button", { name: "获取模型列表" }));
  expect(await screen.findByRole("status")).toHaveTextContent("该服务不提供模型列表，请手动填写模型 ID。");
  expect(screen.queryByRole("alert")).toBeNull();
});

it("does not offer model discovery for audio transcription servers", async () => {
  render(<PersonalConnectionForm id="draft" busy={false} save={vi.fn()} />);
  await userEvent.selectOptions(screen.getByLabelText("接口协议"), "openai_audio");
  expect(screen.queryByRole("button", { name: "获取模型列表" })).toBeNull();
});

it("opens one service at a time while keeping other connection names in view", async () => {
  const second = { ...yamlConnection, connectionId: "other", name: "Batch service" };
  vi.spyOn(api, "getPersonalModelSettings").mockResolvedValue({ defaultModelId: "default", connections: [yamlConnection, second] });
  render(<AnswerModelSettings active />);
  const first = await screen.findByRole("button", { name: yamlConnection.name });
  const batch = screen.getByRole("button", { name: second.name });
  expect(first).toHaveAttribute("aria-expanded", "true");
  expect(batch).toHaveAttribute("aria-expanded", "false");
  await userEvent.click(batch);
  expect(first).toHaveAttribute("aria-expanded", "false");
  expect(batch).toHaveAttribute("aria-expanded", "true");
});

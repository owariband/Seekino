import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../../api/client";
import type { PersonalModelConnection, PersonalModelSettings } from "../../api/contracts";
import { configuredModels } from "../../test/personalModelFixtures";
import { AnswerModelSettings } from "./AnswerModelSettings";
import { preferredChatModel, rememberChatModel, savedChatModelId } from "./usePersonalChatModels";

vi.mock("../../app/workspace", () => ({ useWorkspace: () => ({ mode: "live" }) }));
vi.mock("../auth/AuthProvider", () => ({ useAuth: () => ({ scopeKey: "local" }) }));

const yamlModel = { id: "yaml:test-primary", modelId: "my/team-model", isYamlDefault: true, aliases: ["default"] };
const yamlConnection: PersonalModelConnection = {
  ...configuredModels.connections[0]!, origin: "yaml", connectionId: "yaml-source", name: "home_proxy", models: [yamlModel], revision: 0,
};

beforeEach(() => { localStorage.clear(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("shows configured YAML connections with a credential mask instead of prompting to set them up again", async () => {
  vi.spyOn(api, "getPersonalModelSettings").mockResolvedValue({ defaultModelId: "default", connections: [yamlConnection] });
  render(<AnswerModelSettings active />);
  const card = await screen.findByRole("article");
  expect(within(card).getByRole("heading", { name: "home_proxy" })).toBeVisible();
  expect(within(card).getByText("来自 YAML")).toBeVisible();
  expect(within(card).getByLabelText("API Key 已配置")).toHaveTextContent("••••••••");
  expect(within(card).getByText("my/team-model")).toBeVisible();
  expect(within(card).getByRole("button", { name: "测试连接" })).toBeEnabled();
  expect(within(card).queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "API Key" })).not.toBeInTheDocument();
  expect((screen.getByRole("combobox", { name: "新对话默认模型" }) as HTMLSelectElement).selectedOptions[0]?.textContent).toContain(yamlModel.modelId);
});

it("displays a saved personal key as a mask and omits it from an unchanged edit", async () => {
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

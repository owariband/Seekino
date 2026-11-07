import type { PersonalModelSettings } from "../api/contracts";

export const firstModel = "personal:test-first";
export const secondModel = "personal:test-second";
export const configuredModels: PersonalModelSettings = {
  defaultModelId: firstModel,
  connections: [{
    connectionId: "test-connection", name: "My API", protocol: "openai_chat", baseUrl: "https://example.test/v1",
    supportsVision: true, hasApiKey: true, revision: 1,
    models: [{ id: firstModel, modelId: "team/first" }, { id: secondModel, modelId: "team/second" }],
  }],
};

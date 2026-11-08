import type { AgentMessageOptions } from "../../api/contracts";

export type ChatModelOption = NonNullable<AgentMessageOptions["chatModels"]>[number];

export function chatModelOptions(options: AgentMessageOptions | null | undefined): ChatModelOption[] {
  const seen = new Set<string>();
  const choices: ChatModelOption[] = [];
  const source = options?.chatModels;
  const models = Array.isArray(source) ? source : [];
  for (const model of models) {
    if (!model || typeof model.id !== "string" || typeof model.label !== "string") continue;
    const id = model.id.trim();
    const label = model.label.trim();
    if (!id || !label || seen.has(id)) continue;
    seen.add(id);
    choices.push({ id, label });
  }
  return choices;
}

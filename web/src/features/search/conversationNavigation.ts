import { listAgentConversations } from "../../api/client";
import type { AgentConversationView } from "../../api/contracts";
import { translate, type Locale } from "../../shared/messages";

/** Dispatched on window to focus the standalone search composer (⌘K / Ctrl K). */
export const FOCUS_SEARCH_EVENT = "seekino:focus-search";

export async function loadConversations(locale: Locale): Promise<AgentConversationView[]> {
  const conversations: AgentConversationView[] = [];
  let cursor: string | undefined;
  while (true) {
    const page = await listAgentConversations(100, cursor);
    conversations.push(...page.items);
    if (!page.hasMore) return conversations;
    if (!page.nextCursor) throw new Error(translate(locale, "对话列表缺少下一页游标"));
    cursor = page.nextCursor;
  }
}

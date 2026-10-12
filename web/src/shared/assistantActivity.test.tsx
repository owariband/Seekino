import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useAssistantBusy, useReportAssistantBusy } from "./assistantActivity";

describe("assistant activity", () => {
  it("stays busy until every reporting assistant has finished", () => {
    const header = renderHook(() => useAssistantBusy());
    const first = renderHook(({ busy }) => useReportAssistantBusy(busy), { initialProps: { busy: true } });
    const second = renderHook(({ busy }) => useReportAssistantBusy(busy), { initialProps: { busy: true } });
    expect(header.result.current).toBe(true);
    act(() => first.rerender({ busy: false }));
    expect(header.result.current).toBe(true);
    act(() => second.unmount());
    expect(header.result.current).toBe(false);
  });
});

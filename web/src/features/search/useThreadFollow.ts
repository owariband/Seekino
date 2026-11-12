import { useCallback, useEffect, useRef, type UIEvent } from "react";

const FOLLOW_THRESHOLD_PX = 80;

/**
 * Keeps a scrolling transcript pinned to the newest content only while the reader is at the bottom.
 * React updates and real height changes (images, cards, expanded reasoning) share one animation
 * frame, and a reader who scrolls up is left alone until they return to the bottom.
 */
export function useThreadFollow<T extends HTMLElement = HTMLDivElement>() {
  const threadRef = useRef<T>(null);
  const followingRef = useRef(true);
  const frameRef = useRef<number | null>(null);

  const scrollToBottomIfFollowing = useCallback(() => {
    if (frameRef.current !== null) return;
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null;
      const thread = threadRef.current;
      if (!thread || !followingRef.current) return;
      thread.scrollTop = thread.scrollHeight;
    });
  }, []);

  const follow = useCallback(() => {
    followingRef.current = true;
    scrollToBottomIfFollowing();
  }, [scrollToBottomIfFollowing]);

  const onScroll = useCallback((event: UIEvent<T>) => {
    const thread = event.currentTarget;
    followingRef.current = thread.scrollHeight - thread.scrollTop - thread.clientHeight < FOLLOW_THRESHOLD_PX;
  }, []);

  useEffect(() => {
    const thread = threadRef.current;
    if (!thread) return undefined;
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(scrollToBottomIfFollowing);
    const observed = new Set<Element>();
    const observeChildren = () => {
      if (!observer) return;
      for (const child of observed) {
        if (thread.contains(child)) continue;
        observer.unobserve(child);
        observed.delete(child);
      }
      for (const child of thread.children) {
        if (observed.has(child)) continue;
        observed.add(child);
        observer.observe(child);
      }
    };
    observer?.observe(thread);
    observeChildren();
    const mutations = typeof MutationObserver === "undefined" ? null : new MutationObserver(observeChildren);
    mutations?.observe(thread, { childList: true });
    return () => {
      mutations?.disconnect();
      observer?.disconnect();
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    };
  }, [scrollToBottomIfFollowing]);

  return { threadRef, follow, onScroll, scrollToBottomIfFollowing };
}

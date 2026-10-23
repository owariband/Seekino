import { type CSSProperties, useEffect, useRef, useState } from "react";

interface PlaybackFrameProps {
  src: string | null;
  /** Integer milliseconds, matching stored playback progress. */
  positionSeconds: number;
  poster?: string | null;
  className?: string;
  eager?: boolean;
}

const imageStyle: CSSProperties = { position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover" };

function Frame({ src, positionSeconds, poster, className, eager = false }: PlaybackFrameProps) {
  const wrapperRef = useRef<HTMLSpanElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [active, setActive] = useState(eager);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (eager || typeof IntersectionObserver === "undefined") { setActive(true); return; }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { setActive(true); observer.disconnect(); }
    });
    if (wrapperRef.current) observer.observe(wrapperRef.current);
    return () => observer.disconnect();
  }, [eager]);

  useEffect(() => {
    const media = videoRef.current;
    if (!active || !src || failed || !media) return;
    let disposed = false;
    let target: number | null = null;
    const fail = () => { if (!disposed) { setReady(false); setFailed(true); } };
    const reveal = () => {
      if (disposed || target === null || media.seeking || media.readyState < 2 || Math.abs(media.currentTime - target) > 0.15) return;
      setReady(true);
    };
    const seek = () => {
      if (!Number.isFinite(media.duration) || media.duration <= 0) { fail(); return; }
      target = Math.min(Math.max(0, Number.isFinite(positionSeconds) ? positionSeconds / 1000 : 0), Math.max(0, media.duration - 0.1));
      try { media.currentTime = target; } catch { fail(); return; }
      reveal();
    };
    media.addEventListener("loadedmetadata", seek);
    media.addEventListener("loadeddata", reveal);
    media.addEventListener("seeked", reveal);
    media.addEventListener("error", fail);
    media.src = src;
    media.load();
    return () => {
      disposed = true;
      media.removeEventListener("loadedmetadata", seek);
      media.removeEventListener("loadeddata", reveal);
      media.removeEventListener("seeked", reveal);
      media.removeEventListener("error", fail);
      media.pause();
      media.removeAttribute("src");
      media.load();
    };
  }, [active, src, positionSeconds, failed]);

  return <span ref={wrapperRef} className={className}>
    {poster ? <img key={poster} src={poster} alt="" style={{ ...imageStyle, visibility: ready ? "hidden" : "visible" }} onError={(event) => { event.currentTarget.style.display = "none"; }} /> : null}
    {active && src && !failed ? <video ref={videoRef} muted playsInline preload={eager ? "auto" : "metadata"} aria-hidden="true" tabIndex={-1}
      style={{ ...imageStyle, visibility: ready ? "visible" : "hidden", opacity: ready ? 1 : 0 }} /> : null}
  </span>;
}

export function PlaybackFrame(props: PlaybackFrameProps) {
  // Remount on source or position changes so a previous frame is never exposed.
  return <Frame key={JSON.stringify([props.src, props.positionSeconds])} {...props} />;
}

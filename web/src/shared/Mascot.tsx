import clsx from "clsx";
import { useId } from "react";

import styles from "./mascot.module.css";

/**
 * idle: tail wag, fin flaps and an occasional blink.
 * seek: looks left and right — loading, searching, answering.
 * puzzled: head tilt with a question mark — empty states and no results.
 * oops: double blink with a sweat drop — recoverable errors.
 */
export type MascotMood = "idle" | "seek" | "puzzled" | "oops" | "still";

const BODY = "M30 158C27 114 58 84 108 80C152 77 197 92 214 124C228 152 221 188 193 203C166 216 120 216 82 211C47 206 32 186 30 158Z";

/** Layered Seekino whale shark; geometry mirrors assets/mascot/seekino-logo.svg. */
export function Mascot({ mood = "idle", size = 96, title, className }: {
  mood?: MascotMood;
  size?: number;
  /** Omit for decorative use next to visible text. */
  title?: string;
  className?: string;
}) {
  const clipId = `seekino-body-${useId().replace(/:/g, "")}`;
  return (
    <svg className={clsx(styles.mascot, className)} data-mood={mood} viewBox="0 0 256 256" width={size} height={size}
      role={title ? "img" : undefined} aria-label={title} aria-hidden={title ? undefined : true} focusable="false">
      <defs><clipPath id={clipId}><path d={BODY} /></clipPath></defs>
      <g fill="none" stroke="var(--mascot-ink)" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round">
        <g fill="var(--mascot-belly)">
          <rect x="63" y="197" width="19" height="26" rx="8.5" />
          <rect x="157" y="199" width="19" height="29" rx="8.5" />
        </g>
        <g className={styles.float}>
          <g className={styles.tilt}>
            <path className={styles.tail} fill="var(--mascot-body)" d="M164 92C174 62 198 36 220 29C230 50 227 80 214 101L233 113C225 119 217 122 208 125Z" />
            <path className={styles.finLeft} fill="var(--mascot-body)" d="M34 134C26 137 19 142 14 148C20 153 27 157 33 159Z" />
            <path fill="var(--mascot-body)" d="M90 88C104 70 120 55 136 48C137 61 140 74 146 87Z" />
            <path fill="var(--mascot-body)" stroke="none" d={BODY} />
            <g clipPath={`url(#${clipId})`} stroke="none">
              <path fill="var(--mascot-shade)" d="M196 122C212 152 210 192 186 230H240V110Z" />
              <path fill="var(--mascot-belly)" d="M18 134C48 114 108 108 154 120C186 130 202 168 192 232H18Z" />
            </g>
            <path d={BODY} />
            <path className={styles.finRight} fill="var(--mascot-body)" d="M204 138C216 142 228 150 234 157C226 161 214 162 205 162" />
            <g className={styles.face}>
              <g className={styles.eyes} fill="var(--mascot-ink)" stroke="none">
                <circle cx="65" cy="113" r="5" />
                <circle cx="155" cy="125" r="5" />
              </g>
              <path strokeWidth="3.6" d="M61 130L151 140" />
            </g>
          </g>
        </g>
        {mood === "puzzled" ? <g className={styles.question} strokeWidth="5">
          <path d="M28 40C28 26 52 24 52 40C52 50 40 50 40 62" />
          <circle cx="40" cy="76" r="2.5" fill="var(--mascot-ink)" />
        </g> : null}
        {mood === "oops" ? <path className={styles.drop} fill="var(--mascot-drop)" strokeWidth="3" d="M32 70C24 82 22 88 22 92C22 98 27 102 32 102C37 102 42 98 42 92C42 88 40 82 32 70Z" /> : null}
      </g>
    </svg>
  );
}

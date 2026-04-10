/// <reference types="astro/client" />

/**
 * Cross-<script> globals bridged across Astro page bundles.
 *
 * Astro scopes <script> bundles per-page, but the TypeScript compiler
 * still sees all pages' declare-global merges during a single check run.
 * Centralising these here avoids copy-pasted Window extensions in every
 * page that exposes a function for an inline onclick or wires a CDN
 * library (qrcode.js, etc.) onto window.
 *
 * Only add a field here when:
 *  - it's assigned on window from page code and read from another bundle
 *    (inline define:vars / onclick attribute), or
 *  - it's a third-party library loaded from a <script src="…"> CDN tag.
 */

declare global {
  interface Window {
    // ── dashboard/new-task.astro — form/json mode bridge ───────────────
    __setMode?: (mode: "form" | "json") => void;
    __savedTplData?: {
      task_type?: string;
      task_input?: unknown;
      task_config?: Record<string, unknown>;
    } | null;

    // ── worker/invites.astro ───────────────────────────────────────────
    respondToInvite?: (
      inviteId: string,
      action: "accept" | "decline",
    ) => Promise<void>;

    // ── worker/certifications.astro ────────────────────────────────────
    submitCert?: () => boolean;

    // ── worker/watchlist.astro ─────────────────────────────────────────
    removeFromWatchlist?: (taskId: string) => Promise<void>;

    // ── workers/browse.astro — invite modals ───────────────────────────
    openInviteModal?: (workerId: string, workerName: string) => void;
    submitInvite?: () => Promise<void>;
    closeBulkModal?: () => void;
    submitBulkInvite?: () => Promise<void>;

    // ── dashboard/experiments.astro — variant builder ──────────────────
    addVariant?: () => void;

    // ── dashboard/security.astro — qrcode.js loaded from CDN ───────────
    QRCode?: {
      toCanvas: (
        canvas: HTMLCanvasElement | null,
        text: string,
        opts: { width: number; margin: number },
      ) => void;
    };
  }
}

// This file must be a module so `declare global` merges into the
// global scope rather than shadowing it.
export {};

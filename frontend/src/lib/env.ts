import "server-only";

import { z } from "zod";

/**
 * Server-side configuration, validated once at module load.
 *
 * Note what is *not* here: there is no `NEXT_PUBLIC_API_URL`. The browser
 * never learns the backend's address, because the browser never talks to it —
 * every request goes through this app's own `/bff` proxy (see
 * `src/app/bff/[...path]/route.ts`). That is what keeps the JWTs in
 * httpOnly cookies the page's JavaScript cannot read.
 *
 * `server-only` makes the boundary a build error rather than a code review:
 * importing this from a client component fails the build instead of quietly
 * inlining the backend URL into a bundle.
 */
const schema = z.object({
  /** FastAPI's base, including the version prefix. Server-side only. */
  API_BASE_URL: z.url().default("http://localhost:8000/api/v1"),

  /**
   * How long to wait on the backend before giving up.
   *
   * Deliberately generous, for two reasons that are easy to underestimate:
   *
   * 1. Neon's free tier cold-starts after idle (CLAUDE.md §5). The first
   *    request of the morning legitimately takes several seconds, and
   *    treating that as an error greets the first receptionist with a red
   *    screen.
   * 2. **A timeout on a write is worse than a slow write.** Registration is
   *    not idempotent: if this fires while the backend is midway through
   *    creating a patient, the record is still created, the counter sees
   *    "cannot reach the server", and the obvious human response is to
   *    register the patient again. A duplicate UHID means a split medical
   *    history — the exact harm the duplicate detector exists to prevent.
   *
   * So this is a backstop against a genuinely dead backend, not a latency
   * budget. If requests are routinely near it, the fix is the database's
   * placement (see the README's note on Neon regions), not a lower number
   * here.
   */
  API_TIMEOUT_MS: z.coerce.number().int().min(1000).max(120000).default(45000),

  NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
});

const parsed = schema.safeParse(process.env);

if (!parsed.success) {
  throw new Error(
    `Invalid frontend environment:\n${z.prettifyError(parsed.error)}\n` +
      "See .env.example at the repository root.",
  );
}

export const env = parsed.data;

/** Secure cookies require TLS, which the local dev server does not have. */
export const isProduction = env.NODE_ENV === "production";

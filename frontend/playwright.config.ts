import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests (CLAUDE.md §4: Playwright for e2e).
 *
 * These run against a **real backend and a real database** — no mocks. That is
 * the point of having them at all: the unit tests already prove each service
 * in isolation, and what remains untested is precisely the seam between the
 * browser, the BFF proxy, FastAPI and Postgres. A mocked e2e suite would pass
 * on the day the session cookie stopped being set.
 *
 * ## Point the API at LOCAL Postgres, not Neon
 *
 * The backend's integration tests already refuse to touch a shared database,
 * and this suite should be run the same way, for the same two reasons plus a
 * third:
 *
 *   * it writes real patients, and a shared branch accumulates them;
 *   * a shared database makes runs interfere with each other;
 *   * **latency swamps the measurement.** A Neon project in `us-east-2` costs
 *     ~870ms per SQL round trip from India, so one registration takes ~30s and
 *     the §7b speed-gate assertion measures the Atlantic rather than the
 *     software. Against local Postgres the same round trip is ~6ms.
 *
 * Before running:
 *
 *   docker compose up -d postgres redis
 *   cd backend
 *   DATABASE_URL=postgresql://hms_app:...@localhost:5432/hospital \
 *   DIRECT_URL=postgresql://hospital:hospital@localhost:5432/hospital \
 *     alembic upgrade head && python scripts/seed_demo.py && uvicorn app.main:app
 *   cd frontend && npm run dev
 *   npm run test:e2e
 *
 * The credentials come from `scripts/seed_demo.py` and are fixed, so the specs
 * can sign in without a fixture that creates users through the API.
 */
export default defineConfig({
  testDir: "./e2e",

  // The specs share one seeded tenant and a single OPD queue. Running them in
  // parallel would have one spec's registration appear in another's queue
  // assertions — flakiness that looks like a bug in the queue.
  fullyParallel: false,
  workers: 1,

  // A failing assertion should never be retried into passing locally: a test
  // that only passes on the second run is a test that has found something.
  retries: process.env.CI ? 1 : 0,

  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"]],

  // Generous. The first request after Neon's free tier auto-suspends pays a
  // cold start of a few seconds (CLAUDE.md §5), and that is not a failure.
  timeout: 60_000,
  expect: { timeout: 15_000 },

  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    // Matches the counter machines this runs on: a 1366×768 laptop, not a
    // designer's 27-inch monitor. Layout problems that only appear on a small
    // screen are the ones that reach staff.
    viewport: { width: 1366, height: 768 },
    locale: "en-IN",
    timezoneId: "Asia/Kolkata",
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});

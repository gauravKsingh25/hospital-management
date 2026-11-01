# Frontend — Next.js 16 · React 19 · Tailwind v4 · shadcn/ui

The staff-facing web app. The full manual is the root [README](../README.md#frontend);
this page gets you running and tells you where things go.

## Run it

```bash
npm install
npm run dev               # http://localhost:3000 — needs the API on API_BASE_URL
```

`.env.local` needs one variable, `API_BASE_URL` (for example
`http://127.0.0.1:8000/api/v1`). It is server-side only: the browser never
talks to FastAPI directly, only to this app's `/bff` proxy.

## Commands

| Command | What it does |
|---|---|
| `npm run dev` | dev server (Turbopack) |
| `npm run verify` | **run before every commit** — codegen check, lint, typecheck, build |
| `npm run codegen` | regenerate API + permission types from the backend |
| `npm run test:e2e` | Playwright, against a real running backend |

## Where code goes

| Folder | Holds | Rule |
|---|---|---|
| `src/app/` | routes only | thin pages: check `can(user, …)`, then render a feature |
| `src/features/<domain>/` | screens, dialogs, hooks for one domain | mirrors a backend module |
| `src/components/ui/` | shadcn primitives | generated — edit sparingly |
| `src/components/` | shared building blocks (`load-error`, `pager`, `status-chip`…) | used by two or more features |
| `src/lib/` | API client, session, permissions, shortcuts | no JSX screens |
| `src/types/` | **generated** API and RBAC types | never hand-edit — run `npm run codegen` |
| `src/i18n/messages/` | `en.json`, `hi.json` | every string in both, same keys |

## Rules that are not preferences

- **No token in the browser.** Sessions live in an httpOnly cookie; `/bff`
  attaches the JWT server-side.
- **`lib/permissions.ts` decides what to render, never what is allowed.** The
  backend enforces access; hiding a button is courtesy, not security.
- **Every list screen has three states**: loading (`Skeleton`), failed
  (`<LoadError />`), and empty. Never treat a failed request as "no rows".
- **No new heavy dependencies.** Ask first; check `next build` output.
- **Read `AGENTS.md`** — this Next.js version differs from older docs.

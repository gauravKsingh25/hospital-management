"use client";

import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { TooltipProvider } from "@/components/ui/tooltip";
import { getQueryClient } from "@/lib/query-client";

/**
 * The client-side providers, in one component.
 *
 * This is the only `"use client"` boundary in the root layout. Everything
 * above it stays a server component, so the shell — nav, headings, the
 * patient banner — renders as HTML with no JavaScript cost (CLAUDE.md §4).
 *
 * Note what is *not* here: no auth provider, no user context, no permission
 * store. The signed-in user is fetched server-side and passed down as props.
 * A client-side auth context would mean shipping the user's permission list
 * into the bundle and inviting components to make access decisions in the
 * browser — decisions the server has already made and will make again.
 */
export function Providers({ children }: { children: ReactNode }) {
  // Called, not constructed inline: `getQueryClient` keeps one client per
  // browser and a fresh one per server render. Constructing here would make a
  // new cache on every re-render and throw away everything in flight.
  const queryClient = getQueryClient();

  return (
    <QueryClientProvider client={queryClient}>
      {/* 400ms: long enough that tooltips do not flicker as somebody sweeps
          the cursor across a toolbar, short enough to feel responsive when
          they actually pause on a control they do not recognise. */}
      <TooltipProvider delay={400}>{children}</TooltipProvider>
    </QueryClientProvider>
  );
}

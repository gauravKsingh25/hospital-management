import { redirect } from "next/navigation";

import { getCurrentUser } from "@/lib/api/server";
import { homePathFor } from "@/lib/permissions";

/**
 * `/` is a signpost, not a screen.
 *
 * CLAUDE.md §7b wants each role to land on its own dashboard. A shared home
 * page with role-specific panels would mean every user paying to render
 * sections they cannot see, and a doctor's most-used screen sitting one click
 * away from where the browser opens. Redirecting costs one request and puts
 * everyone directly on the screen they came to use.
 */
export default async function IndexPage() {
  const user = await getCurrentUser();
  redirect(homePathFor(user));
}

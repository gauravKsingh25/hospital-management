import Link from "next/link";
import type { ComponentProps } from "react";
import type { VariantProps } from "class-variance-authority";

import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * A link that looks like a button.
 *
 * Deliberately **not** `<Button render={<Link />}>`, which is what this
 * replaced. Base UI's `Button` warned about every one of those in development,
 * and the warning was right: it renders an `<a>`, and neither setting of the
 * `nativeButton` prop is correct for one.
 *
 * - `nativeButton` left at its default `true` — what we had — makes Base UI
 *   put `type="button"` on an anchor, where `type` means the MIME type of the
 *   linked resource and so is meaningless, and skips its link handling.
 * - `nativeButton={false}` is what the warning suggests, and it is worse here:
 *   it adds `role="button"`. These elements *navigate*, so announcing them as
 *   buttons is a lie to a screen reader, drops them out of the browser's link
 *   list, and would break every `getByRole("link")` in the e2e suite.
 *
 * The resolution is that a navigating control does not need button behaviour
 * at all — only button *appearance*. So this takes the styling from
 * `buttonVariants` and leaves the anchor alone: correct `link` semantics,
 * native Ctrl-click and open-in-new-tab, no invented attributes, and nothing
 * for Base UI to warn about because Base UI is not involved.
 *
 * `data-slot="button"` is kept because sibling styles select on it.
 */
export function LinkButton({
  className,
  variant,
  size,
  ...props
}: ComponentProps<typeof Link> & VariantProps<typeof buttonVariants>) {
  return (
    <Link
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  );
}

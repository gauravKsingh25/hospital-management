"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";

import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { Patient } from "@/types/api";

/**
 * A scanned UHID, resolved and opened.
 *
 * Shared by the two scan paths — the counter scanner watched by `useScanner`,
 * and the camera in `CameraScanButton` — because what a scan *does* must not
 * depend on which device read it. Two copies of this would drift the first
 * time one of them learned something the other did not.
 *
 * Straight to `/patients/by-uhid/…` rather than through the search box: a scan
 * is an exact identifier, and running it through a substring search to present
 * a list of one would be slower and less certain.
 *
 * An unrecognised card says so. A scan that silently does nothing is the worst
 * outcome — the receptionist scans again, harder, and concludes the software
 * is broken.
 */
export function useResolveScan(): { resolve: (uhid: string) => void; isResolving: boolean } {
  const t = useTranslations("search");
  const router = useRouter();

  const mutation = useMutation({
    mutationFn: (uhid: string) => api.get<Patient>(`/patients/by-uhid/${uhid}`),
    onSuccess: (found) => router.push(`/patients/${found.id}`),
    onError: (error) =>
      toast.error(
        isApiError(error) && error.status === 404
          ? t("cardNotFound")
          : isApiError(error)
            ? error.message
            : String(error),
      ),
  });

  // `mutate` is referentially stable across renders, so callers can depend on
  // `resolve` in an effect without tearing the camera down every render.
  return { resolve: mutation.mutate, isResolving: mutation.isPending };
}

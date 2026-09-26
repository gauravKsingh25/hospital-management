import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { BedBoard } from "@/features/wards/bed-board";
import { getCurrentUser, serverFetch, serverFetchOptional } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { BoardWard, Occupancy } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("wards");
  return { title: t("title") };
}

/**
 * The nursing station's bed board.
 *
 * Server-rendered so the first paint already holds the ward. This screen is
 * opened at the start of a shift and left up on a station monitor all day, so
 * the initial render is the one that has to be right; the client takes over
 * polling.
 */
export default async function WardsPage() {
  const t = await getTranslations("wards");
  const user = await getCurrentUser();

  if (!can(user, "bed:read")) return <NoPermission />;

  const [board, occupancy] = await Promise.all([
    serverFetch<BoardWard[]>("/ipd/wards/board"),
    // Optional: the occupancy strip is management's number, and a nurse
    // without it should still get the board rather than an error page.
    serverFetchOptional<Occupancy>("/ipd/wards/occupancy"),
  ]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-muted-foreground text-sm">{t("subtitle")}</p>
      </div>

      <BedBoard
        initialBoard={board}
        initialOccupancy={occupancy}
        canClean={can(user, "bed:clean")}
        canBlock={can(user, "bed:block")}
      />
    </div>
  );
}

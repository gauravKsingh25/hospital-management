"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";
import { Search, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { formatMoney } from "@/features/billing/money";
import { useMedicines } from "@/features/medicines/use-medicines";
import { cn } from "@/lib/utils";
import type { MedicineItem } from "@/types/api";

/**
 * Pick one item from the medicine list.
 *
 * A search box over a short result list rather than a dropdown: the list runs
 * to hundreds of items, and "cef" should find ceftriaxone in two keystrokes.
 * Arrow keys and Enter work, so a keyboard user never reaches for the mouse.
 *
 * Once something is picked it collapses to a summary — name, dose, price —
 * with a way to change it, so the rest of the form has room.
 */
export function MedicinePicker({
  label,
  value,
  onChange,
  autoFocus = false,
}: {
  label: string;
  value: MedicineItem | null;
  onChange: (item: MedicineItem | null) => void;
  autoFocus?: boolean;
}) {
  const t = useTranslations("medicines");
  const common = useTranslations("common");
  const inputId = useId();
  const listId = useId();
  const [term, setTerm] = useState("");
  const [active, setActive] = useState(0);

  const results = useMedicines({ search: term, enabled: value === null, limit: 8 });
  const options = results.data?.items ?? [];
  const highlighted = Math.min(active, Math.max(options.length - 1, 0));

  if (value) {
    return (
      <div className="space-y-1.5">
        <span className="text-sm font-medium">{label}</span>
        <div
          className="border-primary/40 bg-primary/5 flex items-center gap-3 rounded-md border px-3 py-2"
          data-testid="picked-medicine"
        >
          <div className="min-w-0 flex-1">
            <div className="truncate font-medium">{value.name}</div>
            <div className="text-muted-foreground text-xs">
              {describe(value, t("formLabel", { form: value.form }))} ·{" "}
              {value.price !== null && value.price !== undefined
                ? t("pricePer", { price: formatMoney(value.price), unit: value.unit })
                : t("notPriced")}
            </div>
          </div>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-tap shrink-0"
            onClick={() => {
              setTerm("");
              onChange(null);
            }}
            aria-label={t("clearPick", { name: value.name })}
          >
            <X aria-hidden className="size-4" />
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      <label htmlFor={inputId} className="text-sm font-medium">
        {label}
      </label>
      <div className="relative">
        <Search
          aria-hidden
          className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
        />
        <Input
          id={inputId}
          value={term}
          onChange={(event) => {
            setTerm(event.target.value);
            setActive(0);
          }}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setActive((index) => Math.min(index + 1, options.length - 1));
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              setActive((index) => Math.max(index - 1, 0));
            } else if (event.key === "Enter" && options[highlighted]) {
              event.preventDefault();
              onChange(options[highlighted]);
            }
          }}
          placeholder={t("searchPlaceholder")}
          className="h-tap pl-9"
          autoFocus={autoFocus}
          autoComplete="off"
          role="combobox"
          aria-expanded={options.length > 0}
          aria-controls={listId}
          aria-activedescendant={options[highlighted] ? `${listId}-${highlighted}` : undefined}
        />
      </div>

      <ul
        id={listId}
        role="listbox"
        aria-label={label}
        className="max-h-60 overflow-y-auto rounded-md border"
      >
        {results.isPending ? (
          <li className="text-muted-foreground px-3 py-2 text-sm">{common("loading")}</li>
        ) : options.length === 0 ? (
          <li className="text-muted-foreground px-3 py-2 text-sm">
            {term.trim() ? t("noMatch", { term: term.trim() }) : t("emptyList")}
          </li>
        ) : (
          options.map((item, index) => (
            <li
              key={item.id}
              id={`${listId}-${index}`}
              role="option"
              aria-selected={index === highlighted}
              onMouseEnter={() => setActive(index)}
              onMouseDown={(event) => {
                // Before the input loses focus, so the pick is never lost to a blur.
                event.preventDefault();
                onChange(item);
              }}
              className={cn(
                "min-h-tap flex cursor-pointer items-center gap-3 px-3 py-2 text-sm",
                index === highlighted && "bg-accent",
              )}
              data-testid="medicine-option"
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{item.name}</span>
                <span className="text-muted-foreground block truncate text-xs">
                  {describe(item, t("formLabel", { form: item.form }))}
                </span>
              </span>
              <span className="tabular shrink-0 text-xs">
                {item.price !== null && item.price !== undefined
                  ? t("pricePer", { price: formatMoney(item.price), unit: item.unit })
                  : t("notPriced")}
              </span>
            </li>
          ))
        )}
      </ul>
    </div>
  );
}

/** "Injection · 1 g · IV" — what the item is, in one line. */
export function describe(item: MedicineItem, formLabel: string): string {
  return [formLabel, item.dose, item.route].filter(Boolean).join(" · ");
}

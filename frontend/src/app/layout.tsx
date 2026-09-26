import type { Metadata, Viewport } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getTranslations } from "next-intl/server";
import { Inter, Noto_Sans_Devanagari } from "next/font/google";

import { Providers } from "@/components/providers";
import { Toaster } from "@/components/ui/sonner";

import "./globals.css";

/**
 * Self-hosted, subset to the scripts in use.
 *
 * `next/font` downloads and serves the font from this origin at build time,
 * which matters for three reasons: the CSP can keep `font-src 'self'`, no
 * request leaves for a third party carrying a hospital's referrer, and a
 * hospital network that blocks Google still renders correctly.
 *
 * Two families, because Inter has no Devanagari coverage. They are stacked in
 * `--font-sans` rather than swapped by locale: a Hindi screen still shows
 * English drug names and UHIDs, and picking a font per page would render
 * those in whatever the browser fell back to. Stacking lets each glyph come
 * from the family that actually has it, in the same line of text.
 */
const inter = Inter({
  subsets: ["latin", "latin-ext"],
  variable: "--font-latin",
  display: "swap",
});

const devanagari = Noto_Sans_Devanagari({
  subsets: ["devanagari"],
  variable: "--font-devanagari",
  display: "swap",
});

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("app");
  return {
    title: { default: t("name"), template: `%s · ${t("shortName")}` },
    description: "Patient journey management",
    // Internal system behind a login. Nothing here should ever be indexed.
    robots: { index: false, follow: false },
  };
}

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // Zoom stays enabled. Pinch-to-zoom is how someone reads a dose on a
  // ward tablet in poor light, and disabling it is an accessibility failure
  // dressed up as polish.
  maximumScale: 5,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f7f8f9" },
    { media: "(prefers-color-scheme: dark)", color: "#1c1f24" },
  ],
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const locale = await getLocale();

  return (
    <html
      lang={locale}
      className={`${inter.variable} ${devanagari.variable} h-full`}
      suppressHydrationWarning
    >
      <body className="bg-background text-foreground flex min-h-full flex-col antialiased">
        {/* Locale and messages are inherited from the server config — see
            `src/i18n/request.ts`. */}
        <NextIntlClientProvider>
          <Providers>
            {children}
            {/* Bottom-centre, and above the fold on a laptop: a confirmation
                in a screen corner is a confirmation nobody at a busy counter
                ever sees. */}
            <Toaster position="bottom-center" richColors closeButton duration={4000} />
          </Providers>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}

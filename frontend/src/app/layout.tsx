import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import AuthGuard from "@/components/auth-guard";
import QueueStopButton from "@/components/queue-stop-button";
import TranslationProgress from "@/components/translation-progress";
import UserMenu from "@/components/user-menu";
import Providers from "./providers";

export const metadata: Metadata = {
  title: "Trans — Traduzione letteraria EN→IT",
  description:
    "Piattaforma locale di traduzione letteraria dall'inglese all'italiano.",
};

/** Voci di navigazione principale (PRD §11.1). */
const NAV: { href: string; label: string }[] = [
  { href: "/", label: "Dashboard" },
  { href: "/progetti", label: "Progetti" },
  { href: "/import", label: "Import" },
  { href: "/entita", label: "Entità" },
  { href: "/segmenti", label: "Segmenti" },
  { href: "/traduzione", label: "Traduzione" },
  { href: "/qa", label: "QA" },
  { href: "/prompt-modelli", label: "Prompt/Modelli" },
  { href: "/anteprima", label: "Anteprima" },
  { href: "/export", label: "Export" },
  { href: "/verifica-libro", label: "Verifica Libro" },
  { href: "/info", label: "Info" },
];

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="it">
      <body>
        <Providers>
          <div className="flex min-h-screen">
            <aside className="sticky top-0 flex h-screen w-56 shrink-0 flex-col overflow-y-auto border-r border-slate-800 bg-slate-900/40">
              <div className="border-b border-slate-800 px-4 py-4">
                <Link href="/" className="text-lg font-bold tracking-tight">
                  Trans
                </Link>
                <p className="mt-0.5 text-xs text-slate-400">
                  Traduzione letteraria EN→IT · 100% locale
                </p>
              </div>
              <nav className="flex-1 space-y-0.5 px-2 py-2" aria-label="Principale">
                {NAV.map((item) => (
                  <Link
                    key={item.href}
                    href={item.href}
                    className="block rounded-md px-3 py-1 text-sm leading-5 text-slate-300 hover:bg-slate-800 hover:text-white"
                  >
                    {item.label}
                  </Link>
                ))}
              </nav>
              <TranslationProgress />
              <QueueStopButton />
              <UserMenu />
              <div className="border-t border-slate-800 px-4 py-3 text-xs text-slate-500">
                Dati e inferenza solo in rete locale
              </div>
            </aside>
            <main className="min-w-0 flex-1 px-8 py-6">
              <AuthGuard>{children}</AuthGuard>
            </main>
          </div>
        </Providers>
      </body>
    </html>
  );
}

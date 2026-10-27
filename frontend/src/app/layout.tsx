import type { Metadata } from "next";
import "./globals.css";
import AuthGuard from "@/components/auth-guard";
import Sidebar from "@/components/sidebar";
import Providers from "./providers";

// Language-neutral: la metadata è lato server, mentre il locale è scelto
// dal client (localStorage / navigator.language) nel LocaleProvider.
export const metadata: Metadata = {
  title: "Free Book Translation — EN→IT literary translation",
  description: "Local-first literary translation platform (EN→IT).",
};

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
            <Sidebar />
            <main className="min-w-0 flex-1 px-8 py-6">
              <AuthGuard>{children}</AuthGuard>
            </main>
          </div>
        </Providers>
      </body>
    </html>
  );
}

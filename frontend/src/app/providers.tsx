"use client";

/** Provider client-side per l'intera app: locale (i18n) + TanStack Query. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { LocaleProvider } from "@/lib/i18n";

export default function QueryProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 5_000,
            refetchOnWindowFocus: false,
            retry: 1,
          },
        },
      })
  );

  return (
    <LocaleProvider>
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    </LocaleProvider>
  );
}

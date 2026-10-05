"use client";

/**
 * Guardia di sessione (PRD §2.1): senza access token ogni pagina di dominio
 * ridirige al login. Il controllo gira lato client al mount (l'app è tutta
 * client-rendered); la pagina /login è ovviamente esclusa.
 *
 * 2026-10-01: quando il backend gira con AUTH_DISABLED=1 (portale aperto,
 * interrogato da /health via probeAuthMode) la guardia è disattivata e
 * l'applicazione è navigabile senza credenziali.
 */

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { isLoggedIn, } from "@/lib/auth";
import { authDisabled, probeAuthMode } from "@/lib/api";

export default function AuthGuard({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    if (pathname === "/login") {
      setChecked(true);
      return;
    }
    if (authDisabled()) {
      setChecked(true);
      return;
    }
    let alive = true;
    probeAuthMode().then((openPortal) => {
      if (!alive) return;
      if (openPortal || isLoggedIn()) {
        setChecked(true);
        return;
      }
      router.replace("/login");
    });
    return () => {
      alive = false;
    };
  }, [pathname, router]);

  if (!checked) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center text-sm text-slate-500">
        Verifica sessione…
      </div>
    );
  }
  return <>{children}</>;
}

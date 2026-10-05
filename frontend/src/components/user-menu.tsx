"use client";

/**
 * Menu utente nella sidebar: identity corrente + logout (PRD §2.1).
 * Nascosto quando la sessione non è presente (pagina di login).
 */

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { getEmail, getRole, isLoggedIn, logoutRequest } from "@/lib/auth";

const ROLE_LABEL_IT: Record<string, string> = {
  admin: "Amministratore",
  project_manager: "Project manager",
  translator: "Traduttore",
  revisor: "Revisore",
  qa_reader: "Lettore QA",
};

export default function UserMenu() {
  const router = useRouter();
  const [logged, setLogged] = useState(false);
  const [email, setEmail] = useState<string | null>(null);
  const [role, setRole] = useState<string | null>(null);

  useEffect(() => {
    setLogged(isLoggedIn());
    setEmail(getEmail());
    setRole(getRole());
  }, []);

  if (!logged) return null;

  async function onLogout() {
    await logoutRequest();
    router.replace("/login");
    router.refresh();
  }

  return (
    <div className="border-t border-slate-800 px-4 py-3 text-xs">
      <p className="truncate font-medium text-slate-300" title={email ?? ""}>
        {email ?? "utente"}
      </p>
      <p className="mt-0.5 text-slate-500">
        {role ? (ROLE_LABEL_IT[role] ?? role) : "ruolo n/d"}
      </p>
      <button
        type="button"
        onClick={onLogout}
        className="mt-2 rounded-md border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-800 hover:text-white"
      >
        Esci
      </button>
    </div>
  );
}

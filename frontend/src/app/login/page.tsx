"use client";

/**
 * Login (PRD §2.1): email + password -> sessione locale (access + refresh).
 * Le credenziali non lasciano la rete locale: il POST va al backend tramite
 * il proxy same-origin /api/v1 (route handler Next).
 */

import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

import { loginRequest } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const res = await loginRequest(email.trim(), password);
      if (res.ok) {
        router.replace("/");
        router.refresh();
      } else {
        setError(res.detail || "Credenziali non valide");
      }
    } catch {
      setError("Backend non raggiungibile");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto mt-24 w-full max-w-sm">
      <h1 className="text-2xl font-bold tracking-tight">Accesso a Trans</h1>
      <p className="mt-1 text-sm text-slate-400">
        Piattaforma locale di traduzione letteraria EN→IT
      </p>
      <form
        onSubmit={onSubmit}
        className="mt-6 space-y-4 rounded-lg border border-slate-800 bg-slate-900/40 p-6"
      >
        <div>
          <label htmlFor="email" className="block text-sm font-medium">
            Email
          </label>
          <input
            id="email"
            type="email"
            required
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-slate-500"
            placeholder="utente@esempio.it"
          />
        </div>
        <div>
          <label htmlFor="password" className="block text-sm font-medium">
            Password
          </label>
          <input
            id="password"
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-slate-500"
          />
        </div>
        {error ? (
          <p role="alert" className="text-sm text-red-400">
            {error}
          </p>
        ) : null}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-md bg-sky-600 px-3 py-2 text-sm font-semibold text-white hover:bg-sky-500 disabled:opacity-50"
        >
          {busy ? "Accesso…" : "Accedi"}
        </button>
      </form>
    </div>
  );
}

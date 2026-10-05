import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-xl text-center">
      <h1 className="text-2xl font-bold text-white">Pagina non trovata</h1>
      <p className="mt-2 text-sm text-slate-400">
        La pagina richiesta non esiste.
      </p>
      <Link href="/" className="btn-primary mt-4 inline-flex">
        Torna alla Dashboard
      </Link>
    </div>
  );
}

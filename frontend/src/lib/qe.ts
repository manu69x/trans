/**
 * Regola del testo ridotto analizzato da QE (§10.2-bis, 2026-09-23).
 *
 * Il delimitatore di frase è il punto. Con almeno due frasi si analizzano la
 * prima e l'ultima; con una sola frase (o senza punti) tutto il segmento.
 * Specchio di backend.verify_handler.head_tail_sentences: le due
 * implementazioni devono restare allineate.
 */
export interface QeSpans {
  /** Prima frase (con il punto finale). */
  head: string;
  /** Ultima frase; vuota se il testo è una frase unica. */
  tail: string;
  /** True se QE ha ricevuto il segmento intero. */
  single: boolean;
}

export function qeSpans(text: string | null | undefined): QeSpans {
  const original = text ?? "";
  const t = original.trim();
  if (!t) return { head: original, tail: "", single: true };
  const lead = original.length - t.length;
  const periods: number[] = [];
  for (let i = 0; i < t.length; i++) {
    if (t[i] === ".") periods.push(i);
  }
  if (periods.length === 0) return { head: original, tail: "", single: true };
  const headEnd = lead + periods[0] + 1;
  const tailStart =
    periods.length >= 2 ? periods[periods.length - 2] + 1 : periods[0] + 1;
  return {
    head: original.slice(0, headEnd),
    tail: original.slice(lead + tailStart, lead + t.length),
    single: false,
  };
}

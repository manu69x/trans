import { qeSpans } from "@/lib/qe";

/** Sfondo verde scuro sul testo passato a QE (prima + ultima frase). */
const HL =
  "bg-green-900/70 text-green-100 rounded-sm transition-colors";

export function QeText({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  const { head, tail, single } = qeSpans(text);
  if (!text) return <span className={className} />;
  if (single) {
    return (
      <span className={className}>
        <mark className={HL}>{text}</mark>
      </span>
    );
  }
  const mid = text.slice(head.length, text.length - tail.length);
  return (
    <span className={className}>
      <mark className={HL}>{head}</mark>
      {mid}
      <mark className={HL}>{tail}</mark>
    </span>
  );
}

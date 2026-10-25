"use client";

/**
 * Scheda Info (§11.1): guida di riferimento della piattaforma.
 *
 * Documentazione statica bilingue: flusso di lavoro end-to-end, logica di
 * struttura (parti/capitoli/paragrafi), segmentazione, entità, traduzione,
 * revisione/QA ed export. I contenuti rispecchiano l'implementazione reale
 * (PRD §5.4, §6, §9-10, §15) e vivono nei dizionari (info.*); i marcatori
 * **bold** e _italic_ sono renderizzati da <Rich/>.
 */

import Link from "next/link";
import type { ReactNode } from "react";

import { useI18n } from "@/lib/i18n";

/** Renderizza **bold** e _italic_ dentro una stringa del dizionario. */
function Rich({ text }: { text: string }) {
  const out: ReactNode[] = [];
  const re = /\*\*(.+?)\*\*|_(.+?)_/g;
  let pos = 0;
  let k = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > pos) out.push(text.slice(pos, m.index));
    if (m[1] != null) out.push(<b key={`b${k++}`}>{m[1]}</b>);
    else out.push(<i key={`i${k++}`}>{m[2]}</i>);
    pos = m.index + m[0].length;
  }
  if (pos < text.length) out.push(text.slice(pos));
  return <>{out}</>;
}

function H({ id, children }: { id: string; children: React.ReactNode }) {
  return (
    <h2 id={id} className="mt-10 scroll-mt-6 text-lg font-bold text-white first:mt-0">
      {children}
    </h2>
  );
}

function Li({ children }: { children: React.ReactNode }) {
  return <li className="ml-5 list-disc text-slate-300">{children}</li>;
}

export default function InfoPage() {
  const { t, dict } = useI18n();
  const info = dict.info;
  const toc = info.toc;
  const TOC: { id: string; label: string }[] = [
    { id: "panoramica", label: toc.panoramica },
    { id: "flusso", label: toc.flusso },
    { id: "struttura", label: toc.struttura },
    { id: "paragrafi", label: toc.paragrafi },
    { id: "entita", label: toc.entita },
    { id: "traduzione", label: toc.traduzione },
    { id: "revisione", label: toc.revisione },
    { id: "export", label: toc.export },
    { id: "operative", label: toc.operative },
  ];

  return (
    <div className="mx-auto max-w-3xl pb-16">
      <h1 className="text-2xl font-bold tracking-tight">{info.title}</h1>
      <p className="mt-2 text-sm text-slate-400">{info.intro}</p>

      {/* indice */}
      <nav className="card mt-6 text-sm">
        <p className="font-semibold text-slate-200">{info.tocLabel}</p>
        <ul className="mt-2 grid grid-cols-2 gap-1 sm:grid-cols-3">
          {TOC.map((item) => (
            <li key={item.id}>
              <a href={`#${item.id}`} className="text-indigo-300 hover:underline">
                {item.label}
              </a>
            </li>
          ))}
        </ul>
      </nav>

      <H id="panoramica">{toc.panoramica}</H>
      <ul className="mt-3 space-y-1.5">
        {info.overview.map((item, i) => (
          <Li key={i}>
            <Rich text={item} />
          </Li>
        ))}
      </ul>

      <H id="flusso">{toc.flusso}</H>
      <ol className="mt-3 space-y-2">
        {info.workflow.map((step, i) => (
          <li key={i} className="flex gap-3 text-slate-300">
            <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-indigo-500/25 text-xs font-bold text-indigo-200">
              {i + 1}
            </span>
            <span>{step}</span>
          </li>
        ))}
      </ol>

      <H id="struttura">{toc.struttura}</H>
      <ul className="mt-3 space-y-1.5">
        {info.structure.map((item, i) => (
          <Li key={i}>
            <Rich text={item} />
          </Li>
        ))}
      </ul>

      <H id="paragrafi">{toc.paragrafi}</H>
      <ul className="mt-3 space-y-1.5">
        {info.paragraphs.map((item, i) => (
          <Li key={i}>
            <Rich text={item} />
          </Li>
        ))}
      </ul>

      <H id="entita">{toc.entita}</H>
      <ul className="mt-3 space-y-1.5">
        {info.entities.map((item, i) => (
          <Li key={i}>
            <Rich text={item} />
          </Li>
        ))}
      </ul>

      <H id="traduzione">{toc.traduzione}</H>
      <ul className="mt-3 space-y-1.5">
        {info.translation.map((item, i) => (
          <Li key={i}>
            <Rich text={item} />
          </Li>
        ))}
      </ul>

      <H id="revisione">{toc.revisione}</H>
      <ul className="mt-3 space-y-1.5">
        {info.review.map((item, i) => (
          <Li key={i}>
            <Rich text={item} />
          </Li>
        ))}
      </ul>

      <H id="export">{toc.export}</H>
      <ul className="mt-3 space-y-1.5">
        {info.exportInfo.map((item, i) => (
          <Li key={i}>
            <Rich text={item} />
          </Li>
        ))}
      </ul>

      <H id="operative">{toc.operative}</H>
      <ul className="mt-3 space-y-1.5">
        {info.operative.map((item, i) => (
          <Li key={i}>
            <Rich text={item} />
          </Li>
        ))}
      </ul>

      <p className="mt-10 text-xs text-slate-500">
        Full references: <code>docs/PRD.md</code>, ADRs in{" "}
        <code>docs/adr/</code>, tests and evidence in{" "}
        <code>docs/TESTING.md</code>.{" "}
        <Link href="/" className="text-indigo-300 hover:underline">
          {t("info.backToDashboard")}
        </Link>
      </p>
    </div>
  );
}

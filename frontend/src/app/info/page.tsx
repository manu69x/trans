"use client";

/**
 * Scheda Info (§11.1): guida di riferimento della piattaforma.
 *
 * Documentazione statica in italiano: flusso di lavoro end-to-end, logica di
 * struttura (parti/capitoli/paragrafi), segmentazione, entità, traduzione,
 * revisione/QA ed export. I contenuti rispecchiano l'implementazione reale
 * (PRD §5.4, §6, §9-10, §15).
 */

import Link from "next/link";

const TOC: { id: string; label: string }[] = [
  { id: "panoramica", label: "Panoramica" },
  { id: "flusso", label: "Flusso di lavoro" },
  { id: "struttura", label: "Parti e capitoli" },
  { id: "paragrafi", label: "Paragrafi e segmenti" },
  { id: "entita", label: "Entità" },
  { id: "traduzione", label: "Traduzione" },
  { id: "revisione", label: "Revisione e QA" },
  { id: "export", label: "Export" },
  { id: "operative", label: "Note operative" },
];

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
  return (
    <div className="mx-auto max-w-3xl pb-16">
      <h1 className="text-2xl font-bold tracking-tight">Info — come funziona Trans</h1>
      <p className="mt-2 text-sm text-slate-400">
        Guida di riferimento alla piattaforma locale di traduzione letteraria
        EN→IT. Tutto gira sulla rete locale: nessun dato del manoscritto esce
        dalla macchina e l'inferenza passa solo dal LLM Gateway.
      </p>

      {/* indice */}
      <nav className="card mt-6 text-sm">
        <p className="font-semibold text-slate-200">Indice</p>
        <ul className="mt-2 grid grid-cols-2 gap-1 sm:grid-cols-3">
          {TOC.map((t) => (
            <li key={t.id}>
              <a href={`#${t.id}`} className="text-indigo-300 hover:underline">
                {t.label}
              </a>
            </li>
          ))}
        </ul>
      </nav>

      <H id="panoramica">Panoramica</H>
      <ul className="mt-3 space-y-1.5">
        <Li>
          <b>Stack</b>: frontend Next.js (:3002), backend FastAPI (:8000),
          PostgreSQL + pgvector, MinIO, Redis. Tutto in Docker Compose, tutto
          locale.
        </Li>
        <Li>
          <b>Accesso</b>: login con email e password (utente admin di
          bootstrap); cinque ruoli (admin, project manager, traduttore,
          revisore, lettore QA) con permessi diversi.
        </Li>
        <Li>
          <b>Principi</b>: le versioni approvate sono immutabili (ogni modifica
          crea una nuova versione); l'output del modello resta{" "}
          <i>bozza automatica</i> finché un umano non lo approva; i log non
          contengono mai testo del manoscritto.
        </Li>
      </ul>

      <H id="flusso">Flusso di lavoro consigliato</H>
      <ol className="mt-3 space-y-2">
        {[
          "Import: crea il progetto, conferma il copyright e carica il PDF. Il documento viene analizzato pagina per pagina (testo con coordinate; OCR automatico per le pagine scansionate, con confidenza).",
          "Struttura: premi «Rileva struttura»: il sistema propone parti e capitoli combinando segnalibri PDF, font e pattern. Controlla le proposte, correggi i confini se serve e conferma i capitoli.",
          "Segmentazione: seleziona i capitoli e premi «Segmenta». Regola «frasi per segmento» se vuoi unità di revisione più grandi. Il colore indica se il capitolo entra in un blocco di traduzione.",
          "Entità: dalla scheda Entità lancia l'estrazione: il sistema propone nomi, luoghi e organizzazioni con le evidenze nel testo. Revisiona e approva: solo le entità approvate vincolano la traduzione.",
          "Glossario e memoria (opzionale): carica un glossario (CSV/TBX) o approva termini; la memoria di traduzione si arricchisce automaticamente con i segmenti approvati.",
          "Traduzione: dalla scheda Traduzione seleziona i segmenti e lancia. Il modello riceve il testo con glossario, entità, memoria e guida di stile; il risultato è una bozza automatica da revisionare.",
          "Revisione e QA: confronta EN/IT, controlla le issue QA, approva o rifiuta i segmenti. L'approvazione è definitiva (versioni immutabili).",
          "Export: esporta il libro (DOCX/EPUB/HTML) solo con i segmenti approvati, oppure i bilingui CAT (XLIFF/TMX/CSV). Le bozze non approvate richiedono il watermark esplicito.",
        ].map((t, i) => (
          <li key={i} className="flex gap-3 text-slate-300">
            <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-indigo-500/25 text-xs font-bold text-indigo-200">
              {i + 1}
            </span>
            <span>{t}</span>
          </li>
        ))}
      </ol>

      <H id="struttura">Parti e capitoli</H>
      <ul className="mt-3 space-y-1.5">
        <Li>
          La struttura di un libro è un albero di tre tipi: <b>preliminari</b>{" "}
          (front matter), <b>parti</b> e <b>capitoli</b>. Ogni nodo copre un
          intervallo di pagine e ha uno stato: <i>proposto</i> (detection) o{" "}
          <i>confermato</i> (validato dall'umano).
        </Li>
        <Li>
          Il rilevamento è <b>multi-segnale</b>: usa i segnalibri del PDF
          (metodo più affidabile), poi tipografia e dimensione del font, poi
          pattern del titolo («Capitolo 3», numeri romani…). Ogni proposta
          mostra confidenza e metodo.
        </Li>
        <Li>
          L'editor struttura permette di creare, rinominare, unire, dividere,
          spostare nodi e correggere i confini; dopo una correzione solo i
          segmenti dipendenti vengono rigenerati. C'è sempre l'undo.
        </Li>
        <Li>
          La pulizia header/footer rileva le righe ripetute (numerazione
          pagina, titoli ricorrenti) e le toglie dal corpo del testo, con
          anteprima e rollback.
        </Li>
      </ul>

      <H id="paragrafi">Paragrafi e segmenti</H>
      <ul className="mt-3 space-y-1.5">
        <Li>
          Il <b>paragrafo</b> è l'unità strutturale del capitolo: i segmenti
          non attraversano mai un confine di paragrafo. Nella scheda Segmenti
          ogni capitolo mostra quanti paragrafi contiene (numero appena prima
          dei token).
        </Li>
        <Li>
          Il <b>segmento</b> è l'unità di revisione e traduzione: per default
          una frase; con «frasi per segmento» (1–9999) puoi accorpare N frasi
          consecutive. Dialoghi a trattino, epigrafi, citazioni, versi e
          interruzioni di scena non vengono mai spezzati.
        </Li>
        <Li>
          Il <b>blocco di traduzione</b> è un'altra cosa: è il gruppo di
          segmenti consecutivi che il planner invia al modello in una chiamata
          (fino a ~11.000 token di sorgente, tetto totale 16.384 con riserve
          per glossario e output). Più segmenti non significano più chiamate.
        </Li>
        <Li>
          Un segmento è <span className="text-emerald-300">verde</span> se
          rientra nel limite per blocco,{" "}
          <span className="text-red-300">rosso</span> se sfora (va diviso con
          lo split o ri-segmentato).
        </Li>
        <Li>
          Quando lanci la segmentazione partono dei job asincroni: il bollino
          giallo mostra «in corso», il messaggio verde segnala il
          completamento e la lista si aggiorna da sola.
        </Li>
      </ul>

      <H id="entita">Entità</H>
      <ul className="mt-3 space-y-1.5">
        <Li>
          L'estrazione (BookNLP su GPU + NER) propone <b>persone, luoghi,
          organizzazioni</b> e altro con: nome canonico, alias, tipo, genere
          referenziale (dedotto dai pronomi) e genere/numero grammaticali
          italiani, più le <b>evidenze</b>: citazioni nel testo con pagina.
        </Li>
        <Li>
          Stati: <i>proposta</i> → revisione umana → <i>approvata</i>.{" "}
          <b>Solo le entità approvate vincolano</b> prompt, validazione e QA:
          le proposte sono solo candidature.
        </Li>
        <Li>
          Durante la traduzione, per ogni blocco entrano nel prompt solo le
          entità approvate effettivamente menzionate (e quelle dei due segmenti
          precedenti), con la loro policy: «non tradurre» (la forma inglese
          resta identica), il target canonico, i termini vietati e il
          genere/numero per l'accordo dell'articolo.
        </Li>
        <Li>
          Dopo la risposta del modello, un validatore controlla che le entità
          menzionate nel testo sorgente siano rese secondo la policy: le
          violazioni non bloccano, ma generano issue QA da revisionare.
        </Li>
        <Li>
          Azioni disponibili: approvazione (anche massiva), merge di duplicati
          (unisce alias), split, correzione di genere/numero e policy. Ogni
          modifica conserva lo storico delle versioni.
        </Li>
      </ul>

      <H id="traduzione">Traduzione</H>
      <ul className="mt-3 space-y-1.5">
        <Li>
          Selezioni i segmenti (o il capitolo) e lanci: il planner impacchetta
          i segmenti in blocchi entro il budget token e per ogni blocco
          costruisce il prompt con testo sorgente numerato, entità e glossario
          approvati, esempi dalla memoria di traduzione, guida di stile e
          contesto precedente (solo per comprensione).
        </Li>
        <Li>
          La risposta del modello è validata: gli errori <b>gravi</b> (ID
          mancanti o extra, marcature residue) scartano la risposta; gli
          errori <b>leggeri</b> (violazioni di entità/glossario, output anomalo)
          salvano comunque la bozza e segnalano l'issue.
        </Li>
        <Li>
          Il risultato è sempre una <b>bozza automatica</b>: diventerà testo
          definitivo solo con l'approvazione umana. Ritradurre lo stesso blocco
          con la stessa configurazione riprende la risposta già salvata (nessun
          costo nuovo).
        </Li>
        <Li>
          I modelli disponibili arrivano dal LLM Gateway (selettore in
          Prompt/Modelli); se un modello configurato non è raggiungibile il
          sistema sceglie il primo disponibile e aggiorna il progetto.
        </Li>
      </ul>

      <H id="revisione">Revisione e QA</H>
      <ul className="mt-3 space-y-1.5">
        <Li>
          L'editor mostra sorgente e bozza affiancati: puoi <b>approvare</b> (il
          testo diventa definitivo, immutabile), <b>rifiutare</b> o chiedere una{" "}
          <b>rifinitura</b> al modello. Ogni modifica crea una nuova versione
          consultabile.
        </Li>
        <Li>
          La <b>QA</b> gira su tre livelli: controlli deterministici (numeri,
          segnaposto, termini vietati…), stima di qualità senza riferimento e
          un «critic» che rilegge il blocco. Le issue sono classificate per
          categoria e gravità e collegate al segmento.
        </Li>
        <Li>
          I segmenti approvati alimentano automaticamente la <b>memoria di
          traduzione</b>: le traduzioni future di testi simili partono coerenti
          con le scelte già approvate.
        </Li>
      </ul>

      <H id="export">Export</H>
      <ul className="mt-3 space-y-1.5">
        <Li>
          <b>Editoriale</b>: DOCX, EPUB, HTML con solo il testo approvato; le
          bozze possono essere incluse solo con watermark esplicito.
        </Li>
        <Li>
          <b>CAT bilingue</b>: XLIFF 2.1, TMX 1.4 e CSV per lavorare fuori casa
          e rientrare (reimport senza toccare gli approvati).
        </Li>
        <Li>
          Ogni export produce un manifest verificabile (versione, timestamp,
          conteggi) e uno snapshot riproducibile.
        </Li>
      </ul>

      <H id="operative">Note operative</H>
      <ul className="mt-3 space-y-1.5">
        <Li>
          App su <b>:3002</b>, API su <b>:8000</b> (health: <code>/health</code>
          ). Login richiesto su tutte le operazioni; l'utente admin di bootstrap
          è definito dalle variabili SEED_ADMIN_* del compose.
        </Li>
        <Li>
          Le azioni costose (traduzione, export, upload) hanno limiti di
          frequenza: se vedi un 429, attendi i secondi indicati.
        </Li>
        <Li>
          La tracciabilità completa delle azioni sensibili (login, approvazioni,
          export, cancellazioni) è nell'audit log del database (append-only).
        </Li>
        <Li>
          Backup consigliato: il database contiene il lavoro di revisione —
          attiva un backup periodico (la funzione di backup versionato e
          cifrato è già integrata).
        </Li>
      </ul>

      <p className="mt-10 text-xs text-slate-500">
        Full references: <code>docs/PRD.md</code>, ADRs in{" "}
        <code>docs/adr/</code>, tests and evidence in{" "}
        <code>docs/TESTING.md</code>. <Link href="/" className="text-indigo-300 hover:underline">Torna alla dashboard</Link>
      </p>
    </div>
  );
}

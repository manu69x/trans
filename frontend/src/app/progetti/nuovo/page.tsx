"use client";

/**
 * Wizard "Nuovo progetto" (PRD §11.1 riga 2):
 * 1. metadati + genere (§9.1) + style guide iniziale;
 * 2. dichiarazione copyright (§13.2, obbligatoria);
 * 3. upload PDF con drag&drop e barra di progresso reale;
 * 4. creazione via API T09 (POST /api/v1/projects + POST /documents).
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useCallback, useRef, useState } from "react";

import { ErrorNote } from "@/components/ui";
import { ApiError, createProject, uploadDocument } from "@/lib/api";
import { GENRE_OPTIONS, QUERY_KEY_PROJECTS } from "@/lib/constants";

type WizardStep = 0 | 1 | 2;

const STYLE_GUIDE_PLACEHOLDER =
  "Esempio: registri formali per la narrazione, dialoghi con vocativi naturali; " +
  "mantenere i nomi propri inglesi non fissati dal glossario; unità di misura " +
  "convertite con nota; citazioni letterarie lasciate in inglese con nota del traduttore.";

export default function NewProjectPage() {
  const router = useRouter();
  const queryClient = useQueryClient();

  const [step, setStep] = useState<WizardStep>(0);
  const [title, setTitle] = useState("");
  const [genre, setGenre] = useState<string>("");
  const [styleGuide, setStyleGuide] = useState("");
  const [copyright, setCopyright] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const createMutation = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("nessun file selezionato");
      const project = await createProject({
        title: title.trim(),
        genre_profile: genre,
      });
      const upload = await uploadDocument({
        projectId: project.id,
        file,
        copyrightConfirmed: copyright,
        onProgress: setProgress,
      });
      return { upload };
    },
    onSuccess: async ({ upload }) => {
      await queryClient.invalidateQueries({ queryKey: [...QUERY_KEY_PROJECTS] });
      router.push(`/progetti/${upload.projectId}`);
    },
    onError: (err) => {
      setError(
        err instanceof ApiError
          ? err.detail
          : err instanceof Error
            ? err.message
            : "Errore imprevisto durante la creazione del progetto"
      );
      setProgress(null);
    },
  });

  const canContinueStep0 =
    title.trim().length > 0 && genre !== "" && !createMutation.isPending;

  const canStartUpload =
    file !== null &&
    file.name.toLowerCase().endsWith(".pdf") &&
    copyright &&
    !createMutation.isPending;

  const pickFile = useCallback((selected: File | null | undefined) => {
    if (selected && selected.type === "application/pdf") {
      setFile(selected);
      setError(null);
    } else if (selected) {
      setError("Formato non supportato: seleziona un file PDF.");
      setFile(null);
    }
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragActive(false);
      pickFile(event.dataTransfer.files?.[0]);
    },
    [pickFile]
  );

  const startUpload = () => {
    setError(null);
    setProgress(0);
    createMutation.mutate();
  };

  const resetAll = () => {
    setStep(0);
    setTitle("");
    setGenre("");
    setStyleGuide("");
    setCopyright(false);
    setFile(null);
    setProgress(null);
    setError(null);
  };

  const steps = ["Dettagli progetto", "Copyright", "Caricamento PDF"];

  return (
    <div className="mx-auto max-w-2xl">
      <h1 className="text-2xl font-bold text-white">Nuovo progetto</h1>

      <ol className="mt-4 flex items-center gap-2 text-xs" aria-label="Fasi del wizard">
        {steps.map((label, index) => (
          <li
            key={label}
            className={`flex items-center gap-2 ${
              index === step
                ? "text-white"
                : index < step
                  ? "text-emerald-300"
                  : "text-slate-500"
            }`}
          >
            <span
              className={`flex h-6 w-6 items-center justify-center rounded-full border ${
                index === step
                  ? "border-indigo-400 bg-indigo-500/20 text-indigo-200"
                  : index < step
                    ? "border-emerald-500 bg-emerald-500/20 text-emerald-300"
                    : "border-slate-700 text-slate-500"
              }`}
              aria-current={index === step ? "step" : undefined}
            >
              {index < step ? "✓" : index + 1}
            </span>
            {label}
          </li>
        ))}
      </ol>

      {error && (
        <div className="mt-4">
          <ErrorNote message={error} />
        </div>
      )}

      {step === 0 && (
        <section className="card mt-4 space-y-4" aria-label="Dettagli progetto">
          <div>
            <label htmlFor="titolo" className="label">
              Titolo del progetto <span className="text-red-400">*</span>
            </label>
            <input
              id="titolo"
              className="input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Es. “The Midnight Library”"
              required
            />
          </div>

          <fieldset>
            <legend className="label">
              Tipo di testo <span className="text-red-400">*</span>{" "}
              <span className="font-normal text-slate-500">(PRD §9.1)</span>
            </legend>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {GENRE_OPTIONS.map((option) => (
                <label
                  key={option.value}
                  className={`cursor-pointer rounded-md border p-3 text-sm transition-colors ${
                    genre === option.value
                      ? "border-indigo-400 bg-indigo-500/10"
                      : "border-slate-700 bg-slate-900 hover:border-slate-500"
                  }`}
                >
                  <input
                    type="radio"
                    name="genere"
                    value={option.value}
                    checked={genre === option.value}
                    onChange={() => setGenre(option.value)}
                    className="sr-only"
                  />
                  <span className="font-medium text-white">{option.label}</span>
                  <span className="mt-1 block text-xs text-slate-400">
                    {option.hint}
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          <div>
            <label htmlFor="style-guide" className="label">
              Style guide iniziale{" "}
              <span className="font-normal text-slate-500">(facoltativa, modificabile)</span>
            </label>
            <textarea
              id="style-guide"
              className="input min-h-28"
              value={styleGuide}
              onChange={(e) => setStyleGuide(e.target.value)}
              placeholder={STYLE_GUIDE_PLACEHOLDER}
            />
          </div>

          <div className="flex justify-end">
            <button
              type="button"
              className="btn-primary"
              disabled={!canContinueStep0}
              onClick={() => setStep(1)}
            >
              Avanti
            </button>
          </div>
        </section>
      )}

      {step === 1 && (
        <section className="card mt-4 space-y-4" aria-label="Dichiarazione copyright">
          <p className="text-sm text-slate-300">
            Per procedere è necessario confermare di possedere o amministrare i
            diritti necessari per elaborare questo testo (PRD §13.2). Il testo
            caricato non viene distribuito né usato per addestramento,
            benchmark o telemetria senza consenso esplicito e separato.
          </p>
          <label className="flex items-start gap-3 rounded-md border border-slate-700 bg-slate-900 p-3 text-sm">
            <input
              type="checkbox"
              checked={copyright}
              onChange={(e) => setCopyright(e.target.checked)}
              className="mt-0.5 h-4 w-4"
              required
            />
            <span>
              Confermo di possedere o amministrare i diritti necessari per
              elaborare il testo che sto per caricare.
            </span>
          </label>
          <div className="flex justify-between">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setStep(0)}
            >
              Indietro
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={!copyright}
              onClick={() => setStep(2)}
            >
              Avanti
            </button>
          </div>
        </section>
      )}

      {step === 2 && (
        <section className="card mt-4 space-y-4" aria-label="Caricamento PDF">
          <div
            role="button"
            tabIndex={0}
            aria-label="Trascina qui il PDF o premi Invio per selezionarlo"
            onClick={() => fileInputRef.current?.click()}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                fileInputRef.current?.click();
              }
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setDragActive(true);
            }}
            onDragLeave={() => setDragActive(false)}
            onDrop={onDrop}
            className={`flex min-h-40 cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed p-6 text-center transition-colors ${
              dragActive
                ? "border-indigo-400 bg-indigo-500/10"
                : "border-slate-700 bg-slate-900/60 hover:border-slate-500"
            }`}
          >
            <p className="text-sm font-medium text-slate-200">
              Trascina qui il PDF del manoscritto
            </p>
            <p className="mt-1 text-xs text-slate-500">
              oppure clicca per selezionarlo · solo file PDF
            </p>
            <input
              ref={fileInputRef}
              type="file"
              accept="application/pdf,.pdf"
              className="sr-only"
              onChange={(e) => pickFile(e.target.files?.[0])}
            />
          </div>

          {file && (
            <p className="text-sm text-slate-300">
              File selezionato: <span className="font-medium text-white">{file.name}</span>{" "}
              ({Math.round(file.size / 1024)} KiB)
            </p>
          )}

          {progress != null && (
            <div>
              <div
                className="h-2 w-full overflow-hidden rounded-full bg-slate-800"
                role="progressbar"
                aria-valuenow={progress}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label="Avanzamento upload"
              >
                <div
                  className="h-full rounded-full bg-indigo-500 transition-all"
                  style={{ width: `${progress}%` }}
                />
              </div>
              <p className="mt-1 text-xs text-slate-400">
                Caricamento… {progress}%
              </p>
            </div>
          )}

          <div className="flex justify-between">
            <button
              type="button"
              className="btn-secondary"
              disabled={createMutation.isPending}
              onClick={() => setStep(1)}
            >
              Indietro
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={!canStartUpload}
              onClick={startUpload}
            >
              {createMutation.isPending
                ? "Creazione in corso…"
                : "Crea progetto e carica PDF"}
            </button>
          </div>
        </section>
      )}
    </div>
  );
}

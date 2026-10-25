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
import { QUERY_KEY_PROJECTS } from "@/lib/constants";
import { useI18n } from "@/lib/i18n";

type WizardStep = 0 | 1 | 2;

/** Valori API dei generi (§9.1) — label e hint dal dizionario. */
const GENRE_VALUES = ["fantascienza", "horror", "rosa", "saggio"] as const;

export default function NewProjectPage() {
  const router = useRouter();
  const { t, dict } = useI18n();
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
      if (!file) throw new Error(t("newProject.noFile"));
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
            : t("newProject.unexpectedError")
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

  const pickFile = useCallback(
    (selected: File | null | undefined) => {
      if (selected && selected.type === "application/pdf") {
        setFile(selected);
        setError(null);
      } else if (selected) {
        setError(t("newProject.badFormat"));
        setFile(null);
      }
    },
    [t]
  );

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

  const steps = [
    t("newProject.stepDetails"),
    t("newProject.stepCopyright"),
    t("newProject.stepUpload"),
  ];

  return (
    <div className="mx-auto max-w-2xl">
      <h1 className="text-2xl font-bold text-white">{t("newProject.title")}</h1>

      <ol className="mt-4 flex items-center gap-2 text-xs" aria-label={t("newProject.wizardAria")}>
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
        <section className="card mt-4 space-y-4" aria-label={t("newProject.stepDetails")}>
          <div>
            <label htmlFor="titolo" className="label">
              {t("newProject.projectTitle")} <span className="text-red-400">*</span>
            </label>
            <input
              id="titolo"
              className="input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder={t("newProject.titlePlaceholder")}
              required
            />
          </div>

          <fieldset>
            <legend className="label">
              {t("newProject.textType")} <span className="text-red-400">*</span>{" "}
              <span className="font-normal text-slate-500">(PRD §9.1)</span>
            </legend>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {GENRE_VALUES.map((value) => {
                const option = dict.newProject.genres[value];
                return (
                  <label
                    key={value}
                    className={`cursor-pointer rounded-md border p-3 text-sm transition-colors ${
                      genre === value
                        ? "border-indigo-400 bg-indigo-500/10"
                        : "border-slate-700 bg-slate-900 hover:border-slate-500"
                    }`}
                  >
                    <input
                      type="radio"
                      name="genere"
                      value={value}
                      checked={genre === value}
                      onChange={() => setGenre(value)}
                      className="sr-only"
                    />
                    <span className="font-medium text-white">{option.label}</span>
                    <span className="mt-1 block text-xs text-slate-400">
                      {option.hint}
                    </span>
                  </label>
                );
              })}
            </div>
          </fieldset>

          <div>
            <label htmlFor="style-guide" className="label">
              {t("newProject.styleGuide")}{" "}
              <span className="font-normal text-slate-500">
                {t("newProject.styleGuideHint")}
              </span>
            </label>
            <textarea
              id="style-guide"
              className="input min-h-28"
              value={styleGuide}
              onChange={(e) => setStyleGuide(e.target.value)}
              placeholder={t("newProject.styleGuidePlaceholder")}
            />
          </div>

          <div className="flex justify-end">
            <button
              type="button"
              className="btn-primary"
              disabled={!canContinueStep0}
              onClick={() => setStep(1)}
            >
              {t("newProject.next")}
            </button>
          </div>
        </section>
      )}

      {step === 1 && (
        <section className="card mt-4 space-y-4" aria-label={t("newProject.stepCopyright")}>
          <p className="text-sm text-slate-300">
            {t("newProject.copyrightBody")}
          </p>
          <label className="flex items-start gap-3 rounded-md border border-slate-700 bg-slate-900 p-3 text-sm">
            <input
              type="checkbox"
              checked={copyright}
              onChange={(e) => setCopyright(e.target.checked)}
              className="mt-0.5 h-4 w-4"
              required
            />
            <span>{t("newProject.copyrightConfirm")}</span>
          </label>
          <div className="flex justify-between">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setStep(0)}
            >
              {t("newProject.back")}
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={!copyright}
              onClick={() => setStep(2)}
            >
              {t("newProject.next")}
            </button>
          </div>
        </section>
      )}

      {step === 2 && (
        <section className="card mt-4 space-y-4" aria-label={t("newProject.stepUpload")}>
          <div
            role="button"
            tabIndex={0}
            aria-label={t("newProject.dropAria")}
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
              {t("newProject.dropTitle")}
            </p>
            <p className="mt-1 text-xs text-slate-500">
              {t("newProject.dropHint")}
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
              {t("newProject.fileSelected")}{" "}
              <span className="font-medium text-white">{file.name}</span>{" "}
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
                aria-label={t("newProject.uploadProgressAria")}
              >
                <div
                  className="h-full rounded-full bg-indigo-500 transition-all"
                  style={{ width: `${progress}%` }}
                />
              </div>
              <p className="mt-1 text-xs text-slate-400">
                {t("newProject.uploading", { p: progress })}
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
              {t("newProject.back")}
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={!canStartUpload}
              onClick={startUpload}
            >
              {createMutation.isPending
                ? t("newProject.createPending")
                : t("newProject.createSubmit")}
            </button>
          </div>
        </section>
      )}
    </div>
  );
}

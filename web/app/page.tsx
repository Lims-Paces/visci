"use client";

/**
 * The premise screen.
 *
 * Deliberately minimal — one input and two choices. Every interesting decision
 * in this product happens downstream, so asking for more here would be friction
 * before the user has seen it do anything.
 *
 * The composer is styled as a single raised surface rather than a stack of
 * labelled form fields: it should feel like a place to write, not a form to
 * fill in.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { type Mode } from "@/lib/api";
import { useConfig, useCreateJob } from "@/lib/queries";

const EXAMPLES = [
  "A lighthouse keeper receives a transmission from a ship that sank forty years ago.",
  "A cartographer discovers a street that appears on no map but her own.",
  "Two strangers realise they have been writing letters to the same dead woman.",
];

export default function Home() {
  const router = useRouter();
  const [premise, setPremise] = useState("");
  const [mode, setMode] = useState<Mode>("dialogue");
  const [real, setReal] = useState(false);
  const [draft, setDraft] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Fetched once and cached forever — config does not change while the app is
  // open, so staleTime: Infinity means no repeat requests.
  const { data: config } = useConfig();

  // A MUTATION, not a query. Mutations write data: they run when called, are
  // never cached, and are never auto-retried — retrying "create film" on a
  // network blip could start two films and charge twice.
  //
  // `isPending` replaces the manual `busy` flag.
  const createJob = useCreateJob();

  const cost = real
    ? draft
      ? config?.estimated_cost_draft ?? 10
      : config?.estimated_cost_full ?? 16
    : 0;

  async function start() {
    if (premise.trim().length < 10) {
      setError("Give it a little more to work with — a sentence or two.");
      return;
    }
    setError(null);
    try {
      // mutateAsync throws on failure so it can be awaited in a try/catch.
      // Its onSuccess (in lib/queries.ts) seeds the job cache and refreshes the
      // sidebar, so the new film appears in both places immediately rather
      // than up to 8 seconds later.
      const job = await createJob.mutateAsync({
        premise: premise.trim(),
        mode,
        real,
        draft,
      });
      router.push(`/job/${job.job_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the film.");
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col justify-center px-5 py-10 sm:px-8 sm:py-16">
      <header className="mb-10">
        <h1 className="text-[34px] font-extrabold leading-[1.05] sm:text-[44px] lg:text-[52px]">
          What&rsquo;s the film?
        </h1>
        <p className="mt-3 text-base text-[var(--ink-soft)] sm:text-lg">
          One idea becomes a two-minute vertical short — script, characters,
          shots, and sound.
        </p>
      </header>

      {/* The composer: one raised surface containing the input and its controls,
          rather than separate boxes. Borrowed from chat interfaces, where the
          input and its options read as a single instrument. */}
      <section className="overflow-hidden rounded-[var(--r-lg)] bg-[var(--surface)] shadow-2xl shadow-black/40">
        <textarea
          value={premise}
          onChange={(e) => setPremise(e.target.value)}
          rows={4}
          placeholder="A lighthouse keeper receives a transmission from a ship that sank forty years ago…"
          aria-label="Your film idea"
          className="w-full resize-none bg-transparent px-4 pb-4 pt-5 text-base leading-relaxed outline-none placeholder:text-[var(--ink-faint)] sm:px-6 sm:pt-6 sm:text-[17px]"
        />

        <div className="flex flex-wrap items-center gap-2 px-3 pb-3 sm:px-4 sm:pb-4">
          <Segmented
            value={mode}
            onChange={setMode}
            options={[
              { value: "dialogue", label: "Dialogue" },
              { value: "narration", label: "Narration" },
            ]}
          />

          <Toggle active={real} onClick={() => setReal(!real)}>
            {real ? "Real generation" : "Free preview"}
          </Toggle>

          {real && (
            <Toggle active={draft} onClick={() => setDraft(!draft)}>
              {draft ? "Draft" : "Full quality"}
            </Toggle>
          )}

          <span className="ml-auto flex items-center gap-2 sm:gap-3">
            <span className="font-mono text-xs text-[var(--ink-faint)] tabular-nums">
              {real ? `~$${cost.toFixed(2)} · 8–15 min` : "free · instant"}
            </span>
            <button
              onClick={start}
              disabled={createJob.isPending}
              className="rounded-full bg-[var(--accent)] px-5 py-2 text-sm font-semibold text-[var(--accent-ink)] transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              {createJob.isPending ? "Starting…" : "Create"}
            </button>
          </span>
        </div>
      </section>

      {real && !config?.has_gemini_key && (
        <p className="mt-4 text-sm text-[var(--bad)]">
          No Gemini key configured — real generation will fail.
        </p>
      )}

      {error && (
        <p role="alert" className="mt-4 text-sm text-[var(--bad)]">
          {error}
        </p>
      )}

      {/* Examples do real work here: most people freeze at an empty box, and a
          concrete premise teaches the shape of a good one faster than a hint. */}
      {!premise && (
        <div className="mt-8">
          <p className="mb-3 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--ink-faint)]">
            Or start from one of these
          </p>
          <div className="space-y-2">
            {EXAMPLES.map((example) => (
              <button
                key={example}
                onClick={() => setPremise(example)}
                className="block w-full rounded-[var(--r-md)] bg-[var(--surface)] px-4 py-3 text-left text-sm text-[var(--ink-soft)] transition-colors hover:bg-[var(--raised)] hover:text-[var(--ink)]"
              >
                {example}
              </button>
            ))}
          </div>
        </div>
      )}
    </main>
  );
}

function Segmented<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string }[];
}) {
  return (
    <div className="flex rounded-full bg-[var(--raised)] p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          aria-pressed={value === o.value}
          className={`rounded-full px-3.5 py-1.5 text-xs font-medium transition-colors ${
            value === o.value
              ? "bg-[var(--hover)] text-[var(--ink)]"
              : "text-[var(--ink-faint)] hover:text-[var(--ink-soft)]"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Toggle({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-full px-3.5 py-1.5 text-xs font-medium transition-colors ${
        active
          ? "bg-[var(--accent)]/15 text-[var(--accent)]"
          : "bg-[var(--raised)] text-[var(--ink-faint)] hover:text-[var(--ink-soft)]"
      }`}
    >
      {children}
    </button>
  );
}

"use client";

/**
 * The job screen — progress, then the finished film.
 *
 * The most important screen in the product. A real generation takes 8-15
 * minutes, and the difference between that feeling reasonable and feeling
 * broken is entirely what happens here.
 *
 * Principle: show the film ASSEMBLING. Keyframes appear as they generate, so
 * the wait reads as work happening rather than a spinner spinning. Every card
 * is a visible piece of the thing being built.
 *
 * The grid is the interface — chrome recedes, media leads. Captions live in a
 * hover overlay rather than always-on, so fifteen cards read as a contact sheet
 * instead of a spreadsheet.
 */

import { use, useEffect, useState } from "react";
import {
  clipUrl,
  exportUrl,
  keyframeUrl,
  videoUrl,
  type Shot,
} from "@/lib/api";
import { useJob } from "@/lib/queries";

const STATUS: Record<Shot["status"], { dot: string; label: string }> = {
  done: { dot: "bg-[var(--good)]", label: "ready" },
  keyframe: { dot: "bg-[var(--warn)] breathe", label: "filming" },
  pending: { dot: "bg-[var(--ink-faint)]", label: "queued" },
  refused: { dot: "bg-[var(--bad)]", label: "skipped" },
};

export default function JobPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [playing, setPlaying] = useState<Shot | null>(null);

  // Replaces ~20 lines of hand-rolled polling.
  //
  // What the hook does that the old loop did not:
  //   - retries on a network blip instead of dying silently. The old poller
  //     stopped forever on one failed request, so a brief wifi drop during a
  //     15-minute generation meant the page never updated again.
  //   - stops polling by itself once the job finishes
  //   - pauses while the tab is in the background
  //   - shares its cache with the sidebar, so both agree instantly
  //   - shows cached data immediately when you navigate back to a film
  const { data: job, error, isPending } = useJob(id);

  if (error) {
    return (
      <div className="px-5 py-12 sm:px-8 lg:px-10">
        <p className="text-[var(--bad)]">
          {error instanceof Error ? error.message : "Lost the server."}
        </p>
      </div>
    );
  }

  if (isPending || !job) {
    return (
      <div className="px-5 py-12 sm:px-8 lg:px-10">
        <p className="breathe text-[var(--ink-faint)]">Loading…</p>
      </div>
    );
  }

  const pct = job.shots_total
    ? Math.round((job.shots_done / job.shots_total) * 100)
    : 0;

  return (
    <div className="px-5 py-6 sm:px-8 lg:px-10 lg:py-10">
      <header className="mb-8 max-w-3xl">
        <h1 className="text-[28px] font-extrabold leading-[1.1] sm:text-[34px] lg:text-[40px]">
          {job.title ?? <span className="breathe">Working on it…</span>}
        </h1>
        {job.logline && (
          <p className="mt-3 text-[15px] leading-relaxed text-[var(--ink-soft)] sm:text-[17px]">
            {job.logline}
          </p>
        )}
      </header>

      {!job.finished && !job.error && (
        <section className="mb-10 max-w-xl">
          <div className="mb-2.5 flex items-baseline justify-between gap-4">
            <span className="breathe text-sm font-medium">{job.stage}</span>
            <span className="font-mono text-xs text-[var(--ink-faint)] tabular-nums">
              {job.shots_total > 0
                ? `${job.shots_done}/${job.shots_total} shots`
                : `step ${job.stage_number} of ${job.stages_total}`}
            </span>
          </div>
          <div className="h-[3px] overflow-hidden rounded-full bg-[var(--raised)]">
            <div
              className="h-full rounded-full bg-[var(--accent)] transition-[width] duration-700 ease-out"
              style={{
                width: `${
                  job.shots_total
                    ? pct
                    : (job.stage_number / job.stages_total) * 100
                }%`,
              }}
            />
          </div>
        </section>
      )}

      {job.error && (
        <section className="mb-10 max-w-2xl rounded-[var(--r-md)] bg-[var(--surface)] p-5">
          <h2 className="mb-1.5 text-base font-semibold text-[var(--bad)]">
            Generation stopped
          </h2>
          <p className="text-sm leading-relaxed text-[var(--ink-soft)]">
            {job.error}
          </p>
          {job.shots_done > 0 && (
            <p className="mt-3 text-sm text-[var(--ink-faint)]">
              {job.shots_done} shots finished and are saved — resuming keeps
              them.
            </p>
          )}
        </section>
      )}

      {job.finished && (
        <section className="mb-12 flex flex-col items-start gap-6 lg:flex-row lg:gap-8">
          <video
            key={job.job_id}
            controls
            playsInline
            src={videoUrl(job.job_id)}
            className="aspect-[9/16] w-full max-w-[320px] self-center rounded-[var(--r-lg)] bg-black shadow-2xl shadow-black/50 lg:max-h-[62vh] lg:w-auto lg:self-auto"
          />
          <dl className="grid w-full grid-cols-2 gap-x-8 gap-y-4 pt-2 lg:w-auto">
            {[
              ["Runtime", `${job.shots_done * 8}s`],
              ["Shots", `${job.shots_done}`],
              ["Format", "720 × 1280"],
              ["Cost", `$${job.cost_usd.toFixed(2)}`],
            ].map(([label, value]) => (
              <div key={label}>
                <dt className="font-mono text-[10px] uppercase tracking-[0.15em] text-[var(--ink-faint)]">
                  {label}
                </dt>
                <dd className="mt-1 font-mono text-lg tabular-nums">{value}</dd>
              </div>
            ))}
            <a
              href={videoUrl(job.job_id)}
              download
              className="col-span-2 mt-2 rounded-full bg-[var(--accent)] px-5 py-2.5 text-center text-sm font-semibold text-[var(--accent-ink)] transition-opacity hover:opacity-90"
            >
              Download film
            </a>
            {/* The MP4 is the deliverable, but the clips are what someone
                re-cuts in a real editor and the reference sheets are what keeps
                a sequel consistent. Exporting everything means owning the work
                rather than renting a video file. */}
            <a
              href={exportUrl(job.job_id)}
              className="col-span-2 rounded-full bg-[var(--raised)] px-5 py-2.5 text-center text-sm font-medium transition-colors hover:bg-[var(--hover)]"
            >
              Export project
            </a>
          </dl>
        </section>
      )}

      {job.shots.length > 0 && (
        <section>
          <div className="mb-4 flex items-baseline gap-3">
            <h2 className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--ink-faint)]">
              Shots
            </h2>
            {job.shots_refused > 0 && (
              <span className="font-mono text-[10px] text-[var(--bad)]">
                {job.shots_refused} skipped by safety filters
              </span>
            )}
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 sm:gap-4 lg:grid-cols-4 xl:grid-cols-5">
            {job.shots.map((shot) => (
              <ShotCard
                key={shot.index}
                shot={shot}
                jobId={job.job_id}
                onPlay={() => setPlaying(shot)}
                // Number by position in the SCRIPT, not by position in the
                // grid. A refused shot still occupies its place in the story,
                // so renumbering around it would make the grid disagree with
                // the shot list and with `visci show`.
                number={shot.index + 1}
              />
            ))}
          </div>
        </section>
      )}

      {playing && (
        <ClipPlayer
          shot={playing}
          jobId={job.job_id}
          onClose={() => setPlaying(null)}
        />
      )}
    </div>
  );
}

/**
 * Plays one shot on its own.
 *
 * Being able to watch an individual clip before the film assembles is how you
 * spot a bad take early — otherwise a poor shot is only discovered in the
 * finished cut, after everything has been paid for.
 */
function ClipPlayer({
  shot,
  jobId,
  onClose,
}: {
  shot: Shot;
  jobId: string;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={`Shot ${shot.index + 1}`}
      className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-6 overflow-y-auto bg-black/85 p-4 backdrop-blur-sm sm:p-8 md:flex-row md:gap-10"
    >
      <video
        autoPlay
        controls
        playsInline
        src={clipUrl(jobId, shot.index)}
        onClick={(e) => e.stopPropagation()}
        className="aspect-[9/16] max-h-[70vh] w-auto max-w-full rounded-[var(--r-lg)] bg-black shadow-2xl sm:max-h-[85vh]"
      />
      <div
        onClick={(e) => e.stopPropagation()}
        className="hidden max-w-xs md:block"
      >
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--ink-faint)]">
          Shot {shot.index + 1} · {shot.framing.replace(/_/g, " ")}
        </p>
        <p className="mt-3 text-sm leading-relaxed text-[var(--ink-soft)]">
          {shot.action}
        </p>
        {shot.line && (
          <p className="mt-4 border-l-2 border-[var(--accent)] pl-3 text-sm italic leading-relaxed">
            {shot.speaker && (
              <span className="not-italic font-medium">{shot.speaker}: </span>
            )}
            &ldquo;{shot.line}&rdquo;
          </p>
        )}
      </div>
    </div>
  );
}

function ShotCard({
  shot,
  jobId,
  number,
  onPlay,
}: {
  shot: Shot;
  jobId: string;
  number: number;
  onPlay: () => void;
}) {
  const status = STATUS[shot.status];
  const playable = shot.has_video && !shot.refused;

  return (
    <figure
      onClick={playable ? onPlay : undefined}
      className={`group relative aspect-[9/16] overflow-hidden rounded-[var(--r-md)] bg-[var(--surface)] ${
        shot.has_keyframe ? "rise" : ""
      } ${playable ? "cursor-pointer" : ""}`}
    >
      {shot.has_keyframe ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={keyframeUrl(jobId, shot.index)}
          alt={shot.action.slice(0, 100)}
          className={`h-full w-full object-cover transition-transform duration-500 group-hover:scale-[1.03] ${
            shot.refused ? "opacity-20 grayscale" : ""
          }`}
        />
      ) : (
        <div className="flex h-full items-center justify-center">
          <span className="font-mono text-3xl text-[var(--ink-faint)] opacity-20 tabular-nums">
            {String(number).padStart(2, "0")}
          </span>
        </div>
      )}

      {playable && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center opacity-0 transition-opacity duration-200 group-hover:opacity-100">
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-black/60 backdrop-blur">
            <svg viewBox="0 0 24 24" fill="white" className="ml-0.5 h-5 w-5">
              <path d="M8 5v14l11-7z" />
            </svg>
          </span>
        </div>
      )}

      {/* Status pill, always visible — it is the thing you scan for. */}
      <div className="absolute left-2.5 top-2.5 flex items-center gap-1.5 rounded-full bg-black/60 px-2 py-1 backdrop-blur">
        <span className={`h-1.5 w-1.5 rounded-full ${status.dot}`} />
        <span className="font-mono text-[10px] text-white/90">
          {status.label}
        </span>
      </div>

      <span className="absolute right-2.5 top-2.5 font-mono text-[10px] text-white/50 tabular-nums">
        {String(number).padStart(2, "0")}
      </span>

      {/* Caption on hover rather than always-on. Fifteen permanent captions turn
          a contact sheet into a spreadsheet; the images should lead. */}
      <figcaption className="pointer-events-none absolute inset-x-0 bottom-0 translate-y-2 bg-gradient-to-t from-black/95 via-black/70 to-transparent p-3 pt-8 opacity-0 transition-all duration-300 group-hover:translate-y-0 group-hover:opacity-100">
        <p className="font-mono text-[9px] uppercase tracking-wider text-white/50">
          {shot.framing.replace(/_/g, " ")}
        </p>
        {shot.line ? (
          <p className="mt-1 line-clamp-3 text-xs italic leading-snug text-white/90">
            &ldquo;{shot.line}&rdquo;
          </p>
        ) : (
          <p className="mt-1 line-clamp-3 text-xs leading-snug text-white/75">
            {shot.action}
          </p>
        )}
      </figcaption>
    </figure>
  );
}

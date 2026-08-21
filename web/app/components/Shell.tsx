"use client";

/**
 * The application shell: a sidebar plus a content area.
 *
 * This is what turns disconnected pages into software. Without it every screen
 * has to re-explain where you are and offer its own way back.
 *
 * RESPONSIVE BEHAVIOUR
 * --------------------
 * The sidebar is permanent from `lg` up, and a slide-over drawer below that.
 * A 248px fixed rail on a 390px phone leaves 142px of content — which is not a
 * narrow layout, it is an unusable one.
 *
 * On small screens a compact top bar carries the menu button and the app name,
 * so there is always a way to reach navigation without it stealing the screen.
 */

import { usePathname } from "next/navigation";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useJobs } from "@/lib/queries";

function Icon({ path, className = "" }: { path: string; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`h-[18px] w-[18px] shrink-0 ${className}`}
      aria-hidden="true"
    >
      <path d={path} />
    </svg>
  );
}

const ICONS = {
  plus: "M12 5v14M5 12h14",
  menu: "M4 7h16M4 12h16M4 17h16",
  close: "M6 6l12 12M18 6L6 18",
};

export default function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);

  // Replaces useState + useEffect + setInterval + manual cleanup.
  //
  // The polling, caching, retry, and teardown all live in the hook now. It
  // also shares one cache entry with anything else reading the film list, so
  // there is no longer a second poll that can disagree with this one.
  const { data: jobs = [] } = useJobs();

  // Close the drawer on navigation — leaving it open over the page the user
  // just chose is the classic mobile-nav annoyance.
  useEffect(() => setOpen(false), [pathname]);

  // Escape closes it, matching every other overlay on the platform.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const nav = (
    <>
      <div className="px-5 pb-6 pt-6">
        <Link href="/" className="font-display text-2xl font-extrabold">
          Visci
        </Link>
      </div>

      <div className="px-3">
        <Link
          href="/"
          className={`flex items-center gap-2.5 rounded-[var(--r-sm)] px-3 py-2.5 text-sm font-medium transition-colors ${
            pathname === "/"
              ? "bg-[var(--accent)] text-[var(--accent-ink)]"
              : "text-[var(--ink-soft)] hover:bg-[var(--raised)] hover:text-[var(--ink)]"
          }`}
        >
          <Icon path={ICONS.plus} />
          New film
        </Link>
      </div>

      {/* The film list IS the navigation — a separate "all films" page would be
          an extra click to reach the same information. */}
      <div className="mt-6 min-h-0 flex-1 overflow-y-auto px-3 pb-4">
        <p className="px-3 pb-2 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--ink-faint)]">
          Films
        </p>

        {jobs.length === 0 ? (
          <p className="px-3 py-2 text-sm text-[var(--ink-faint)]">Nothing yet.</p>
        ) : (
          <ul className="space-y-0.5">
            {jobs.map((job) => {
              const active = pathname === `/job/${job.job_id}`;
              return (
                <li key={job.job_id}>
                  <Link
                    href={`/job/${job.job_id}`}
                    className={`block rounded-[var(--r-sm)] px-3 py-2 transition-colors ${
                      active
                        ? "bg-[var(--raised)] text-[var(--ink)]"
                        : "text-[var(--ink-soft)] hover:bg-[var(--raised)]"
                    }`}
                  >
                    <span className="block truncate text-sm">
                      {job.title ?? job.premise}
                    </span>
                    <span className="mt-0.5 flex items-center gap-1.5">
                      {!job.finished && (
                        <span className="h-1.5 w-1.5 rounded-full bg-[var(--warn)] breathe" />
                      )}
                      <span className="font-mono text-[10px] text-[var(--ink-faint)] tabular-nums">
                        {job.finished ? "2:00" : "in progress"}
                        {job.cost_usd > 0 && ` · $${job.cost_usd.toFixed(2)}`}
                      </span>
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </>
  );

  return (
    <div className="min-h-screen">
      {/* Permanent rail, large screens only. */}
      <nav className="fixed left-0 top-0 z-30 hidden h-screen w-[248px] flex-col border-r border-[var(--line)] bg-[var(--surface)] lg:flex">
        {nav}
      </nav>

      {/* Compact bar, small screens only. Sticky so navigation stays reachable
          without scrolling back up a long shot grid. */}
      <header className="sticky top-0 z-20 flex items-center gap-3 border-b border-[var(--line)] bg-[var(--ground)]/90 px-4 py-3 backdrop-blur lg:hidden">
        <button
          onClick={() => setOpen(true)}
          aria-label="Open menu"
          aria-expanded={open}
          className="rounded-[var(--r-sm)] p-1.5 text-[var(--ink-soft)] transition-colors hover:bg-[var(--raised)] hover:text-[var(--ink)]"
        >
          <Icon path={ICONS.menu} />
        </button>
        <Link href="/" className="font-display text-lg font-extrabold">
          Visci
        </Link>
      </header>

      {/* Drawer. Rendered always so it can transition, hidden from assistive
          tech and pointer events when closed. */}
      <div
        className={`fixed inset-0 z-40 lg:hidden ${
          open ? "" : "pointer-events-none"
        }`}
        aria-hidden={!open}
      >
        <div
          onClick={() => setOpen(false)}
          className={`absolute inset-0 bg-black/60 transition-opacity duration-200 ${
            open ? "opacity-100" : "opacity-0"
          }`}
        />
        <nav
          className={`absolute left-0 top-0 flex h-full w-[280px] max-w-[85vw] flex-col border-r border-[var(--line)] bg-[var(--surface)] transition-transform duration-250 ease-out ${
            open ? "translate-x-0" : "-translate-x-full"
          }`}
        >
          <button
            onClick={() => setOpen(false)}
            aria-label="Close menu"
            className="absolute right-3 top-5 rounded-[var(--r-sm)] p-1.5 text-[var(--ink-faint)] hover:bg-[var(--raised)] hover:text-[var(--ink)]"
          >
            <Icon path={ICONS.close} />
          </button>
          {nav}
        </nav>
      </div>

      {/* Content clears the rail only where the rail exists. */}
      <div className="min-w-0 lg:ml-[248px]">{children}</div>
    </div>
  );
}

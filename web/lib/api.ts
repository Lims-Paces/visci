/**
 * Client for the Visci API.
 *
 * These types mirror the Pydantic models in `visci/api.py`. Keeping them in one
 * file means there is a single place to update when the backend schema changes
 * — and eventually a single place to replace with types generated from
 * FastAPI's OpenAPI schema, which removes the drift risk entirely.
 */

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Mode = "dialogue" | "narration";

/**
 * How long a film is — and therefore what kind of story it tells.
 *
 * Not merely a duration: a 4-shot short is a single charged moment, while a
 * 15-shot film has a three-act shape. The backend branches its story prompts
 * on this, so it is a real content choice rather than a slider.
 */
export type Format = "short" | "film";

export interface FormatInfo {
  label: string;
  shot_count: number;
  runtime_seconds: number;
  cost_draft: number;
  cost_full: number;
}

/** How far along one shot is. Drives the status pill in the shot grid. */
export type ShotState = "pending" | "keyframe" | "done" | "refused";

export interface Shot {
  index: number;
  framing: string;
  action: string;
  line: string | null;
  speaker: string | null;
  characters: string[];
  status: ShotState;
  has_keyframe: boolean;
  has_video: boolean;
  refused: boolean;
}

export interface JobStatus {
  job_id: string;
  premise: string;
  mode: Mode;
  format: Format;
  runtime_seconds: number;
  stage: string;
  stage_number: number;
  stages_total: number;
  title: string | null;
  logline: string | null;
  shots: Shot[];
  shots_done: number;
  shots_total: number;
  shots_refused: number;
  real: boolean;
  draft: boolean;
  remaining_cost_draft: number;
  remaining_cost_full: number;
  cost_usd: number;
  finished: boolean;
  error: string | null;
}

export interface JobSummary {
  job_id: string;
  title: string | null;
  premise: string;
  mode: Mode;
  runtime_seconds: number;
  finished: boolean;
  cost_usd: number;
}

export interface ServerConfig {
  has_anthropic_key: boolean;
  has_gemini_key: boolean;
  formats: Record<Format, FormatInfo>;
  default_format: Format;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) {
    // FastAPI puts human-readable messages in `detail`. Surfacing that rather
    // than a bare status code is the difference between "400 Bad Request" and
    // "GEMINI_API_KEY is empty".
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
  }
  return res.json();
}

export function createJob(input: {
  premise: string;
  mode: Mode;
  format: Format;
  real: boolean;
  draft: boolean;
}) {
  return request<JobStatus>("/api/jobs", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export const getJob = (id: string) => request<JobStatus>(`/api/jobs/${id}`);

/**
 * Continue a job that stopped partway.
 *
 * Everything already generated is kept — script, reference sheets, finished
 * clips. Only the missing shots are regenerated.
 */
export const resumeJob = (id: string, draft?: boolean) =>
  request<JobStatus>(
    // The server resumes a job the way it started (free preview or real), so
    // only the video tier is ever sent — never `real`.
    `/api/jobs/${id}/resume${draft === undefined ? "" : `?draft=${draft}`}`,
    { method: "POST" },
  );
export const listJobs = () => request<JobSummary[]>("/api/jobs");
export const getConfig = () => request<ServerConfig>("/api/config");

/** Direct URLs — used as `src` on <video> and <img>, not fetched by us. */
export const videoUrl = (id: string) => `${API}/api/jobs/${id}/video`;
export const keyframeUrl = (id: string, index: number) =>
  `${API}/api/jobs/${id}/keyframe/${index}`;
export const clipUrl = (id: string, index: number) =>
  `${API}/api/jobs/${id}/clip/${index}`;

/** The whole project as a zip: film, clips, keyframes, characters, script. */
export const exportUrl = (id: string) => `${API}/api/jobs/${id}/export`;

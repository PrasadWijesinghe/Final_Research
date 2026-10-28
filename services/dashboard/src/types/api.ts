// Response shapes that belong to the API itself (services/api/src/api/schemas.py).
// Contract types come from contracts.gen.ts (run `npm run gen:types`).
import type { Diagnosis, ReviewDecision, RiskOutput, RoutedFailure } from "./contracts.gen";

export type { Diagnosis, ReviewDecision, RiskOutput, RoutedFailure };
export type Expert = "dependency" | "build" | "test" | "infra";

export interface RunItem {
  context_id: string;
  repo: string;
  head_sha: string;
  event: string;
  history_cutoff: string;
  predictions: RiskOutput[];
  run_id: number | null;
  run_status: string | null;
  run_conclusion: string | null;
}

export interface CommitRisks {
  head_sha: string;
  risks: RiskOutput[];
}

export interface FailureView {
  failure: RoutedFailure;
  diagnoses: Diagnosis[];
  reviews: ReviewDecision[];
}

export interface FailureItem {
  failure_id: string;
  repo: string;
  head_sha: string;
  run_id: number;
  job_name: string;
  expert: Expert;
  created_at: string;
  diagnosis_count: number;
  latest_review: "accept" | "reject" | "modify" | "request_revision" | null;
}

export interface ReviewAck {
  decision_id: string;
  forwarded: boolean;
}

export interface Me {
  login: string;
  avatar_url: string;
  user_hash: string;
}

export interface ApiKeyItem {
  id: string;
  created_at: string;
  revoked_at: string | null;
}

export interface ApiKeyIssued {
  id: string;
  key: string;
  created_at: string;
}

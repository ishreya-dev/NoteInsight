export type AsyncStatus =
  | "idle"
  | "loading"
  | "success"
  | "error";

export type DocumentationStatus =
  | "well_documented"
  | "ambiguous"
  | "mentioned_without_assessment_or_plan";

export type ConditionReviewStatus =
  | "accepted"
  | "edited"
  | "rejected"
  | "added";

export type NoteReviewStatus = "pending" | "reviewed";

export interface Condition {
  id: string;
  condition_name: string;
  evidence_quote: string;
  documentation_status: DocumentationStatus;
  suggested_icd10: string;
  confidence: number;
  quote_verified: boolean;
}

export interface DocumentationGap {
  description: string;
  related_condition: string | null;
}

export interface Analysis {
  id: string;
  note_id: string;
  user_id: string;
  conditions: Condition[];
  gaps: DocumentationGap[];
  summary: string;
  model_version: string;
  prompt_version: string;
  created_at: string;
  is_failed: boolean;
  failure_reason: string | null;
}

export interface Note {
  id: string;
  user_id: string;
  raw_text: string;
  pseudonym: string | null;
  visit_date: string | null;
  created_at: string;
  latest_analysis_id: string | null;
  review_status: NoteReviewStatus;
  condition_count: number;
}

export interface NoteListItem {
  id: string;
  pseudonym: string | null;
  visit_date: string | null;
  created_at: string;
  review_status: NoteReviewStatus;
  condition_count: number;
}

export interface ConditionReviewInput {
  source_condition_id: string | null;
  condition_name: string;
  evidence_quote: string;
  documentation_status: DocumentationStatus;
  suggested_icd10: string;
  status: ConditionReviewStatus;
}

export interface ReviewCreate {
  conditions: ConditionReviewInput[];
  gaps: DocumentationGap[];
  notes: string | null;
}

export interface Review {
  id: string;
  analysis_id: string;
  note_id: string;
  user_id: string;
  conditions: ConditionReviewInput[];
  gaps: DocumentationGap[];
  reviewer_notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface NoteCreateResponse {
  note: Note;
  job_id: string;
}

export interface AnalysisStreamComplete {
  note_id: string;
  analysis: Analysis;
}

export interface AnalysisStreamStatus {
  stage: string;
  message: string;
}

export interface NoteDetailResponse {
  note: Note;
  analysis: Analysis | null;
  review: Review | null;
}

export interface NoteHistoryResponse {
  items: NoteListItem[];
}

export interface AnalysisDetailResponse {
  analysis: Analysis;
  review: Review | null;
}

export interface NoteCreatePayload {
  raw_text: string;
  pseudonym: string | null;
  visit_date: string | null;
}
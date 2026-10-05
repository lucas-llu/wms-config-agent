export type Workspace = { workspace_id: string; role: string };
export type Profile = {
  user_id: string;
  nickname: string;
  status: string;
  email?: string;
  email_verified?: boolean;
  workspaces: Workspace[];
};
export type Session = {
  session_id: string;
  goal: string;
  display_title?: string | null;
  status: string;
  current_revision: number;
};
export type Citation = {
  evidence_id?: string;
  source: string;
  page_start?: number;
  page_end?: number;
  excerpt: string;
  image_ids?: string[];
  images?: string[];
};
export type Turn = {
  turn_id: string;
  role: string;
  message: string;
  revision: number;
  citations?: Citation[];
  legacy_evidence?: string;
  metadata?: { answer_strategy?: string };
};
export type Task = {
  task_id: string;
  title: string;
  module?: string;
  steps?: string[];
  validation_steps?: string[];
  rollback_steps?: string[];
  risk_level?: string;
};
export type State = {
  status: string;
  open_questions?: { text: string }[];
  configuration_tasks?: Task[];
  conversation_summary?: string;
  confirmed_context?: Record<string, unknown>;
  validation_findings?: {
    message?: string;
    description?: string;
    severity?: string;
  }[];
};
export type Workbench = {
  session: Session;
  revision: number;
  state: State;
  turns: Turn[];
  approvals: {
    revision: number;
    decision: string;
    actor: string;
    comment: string;
  }[];
};
export type Device = {
  id: string;
  current: boolean;
  browser: string;
  ip_address: string;
  started: number;
  last_access: number;
};
export type Export = {
  export_id: string;
  revision: number;
  format: string;
  fingerprint: string;
};
export interface Auth {
  token(): Promise<string>;
  login(): Promise<void>;
  register(): Promise<void>;
  recover(): Promise<void>;
  changePassword(): Promise<void>;
  logout(): Promise<void>;
}

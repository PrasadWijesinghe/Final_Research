export type DependencyPredictionLabel = "DEPENDENCY_FAILURE" | "OTHER_FAILURE";

export interface DiagnosticSignal {
  id: string;
  label: string;
  detected: boolean;
  description: string;
}

export interface LogEvidence {
  title: string;
  fileName: string;
  startLine: number;
  excerpt: string;
  highlightedLines: number[];
}

export interface DependencyAnalysisResult {
  id: string;
  caseName: string;
  repository: string;
  runId: string;
  prediction: DependencyPredictionLabel;
  confidence: number;
  signals: DiagnosticSignal[];
  evidence: LogEvidence;
  recommendation: {
    summary: string;
    steps: string[];
  };
}

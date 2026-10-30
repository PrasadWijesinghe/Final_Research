import type { DependencyAnalysisResult, DiagnosticSignal } from "./types";

const signalSet = (detectedIds: string[]): DiagnosticSignal[] => {
  const detected = new Set(detectedIds);
  return [
    {
      id: "install_error",
      label: "Install / resolution error",
      detected: detected.has("install_error"),
      description: "A package-manager install or dependency-resolution step reported an error.",
    },
    {
      id: "missing_module",
      label: "Missing package / module",
      detected: detected.has("missing_module"),
      description: "A required package or module could not be found or imported.",
    },
    {
      id: "test_failure",
      label: "Test failure",
      detected: detected.has("test_failure"),
      description: "A test command reported one or more failing tests or assertions.",
    },
    {
      id: "coverage_error",
      label: "Coverage threshold error",
      detected: detected.has("coverage_error"),
      description: "The measured test coverage did not meet a configured threshold.",
    },
    {
      id: "lint_type_error",
      label: "Lint / type-check error",
      detected: detected.has("lint_type_error"),
      description: "A linter or type checker reported one or more errors.",
    },
    {
      id: "test_coverage_lint_error",
      label: "Test / coverage / lint (combined)",
      detected: detected.has("test_coverage_lint_error"),
      description: "At least one test, coverage, or lint/type-check indicator was detected.",
    },
    {
      id: "ci_permission_error",
      label: "CI permission error",
      detected: detected.has("ci_permission_error"),
      description: "The workflow or an action reported missing authorization or permissions.",
    },
    {
      id: "external_network_error",
      label: "External network error",
      detected: detected.has("external_network_error"),
      description: "A network, DNS, or connection failure was reported in the log.",
    },
    {
      id: "peer_dependency_conflict",
      label: "Peer dependency conflict",
      detected: detected.has("peer_dependency_conflict"),
      description: "Package versions require incompatible peer dependencies.",
    },
    {
      id: "unsupported_toolchain",
      label: "Unsupported toolchain / engine",
      detected: detected.has("unsupported_toolchain"),
      description: "The selected runtime or package engine is outside the supported range.",
    },
    {
      id: "missing_native_bindings",
      label: "Missing native bindings",
      detected: detected.has("missing_native_bindings"),
      description: "A package could not load a required platform-specific native module.",
    },
    {
      id: "build_lifecycle_error",
      label: "Build / lifecycle error",
      detected: detected.has("build_lifecycle_error"),
      description: "A package install, build, or lifecycle script failed.",
    },
    {
      id: "package_integrity_mismatch",
      label: "Package integrity mismatch",
      detected: detected.has("package_integrity_mismatch"),
      description: "Downloaded package content did not match the expected integrity value.",
    },
    {
      id: "package_manager_cache_miss",
      label: "Package-manager cache miss",
      detected: detected.has("package_manager_cache_miss"),
      description: "The log indicates that a package-manager cache or store was not found.",
    },
  ];
};

export const dependencyMockCases: DependencyAnalysisResult[] = [
  {
    id: "case-a",
    caseName: "Peer conflict + install script",
    repository: "acme/web-client",
    runId: "run 4829137041",
    prediction: "DEPENDENCY_FAILURE",
    confidence: 0.885,
    signals: signalSet(["install_error", "peer_dependency_conflict", "build_lifecycle_error"]),
    evidence: {
      title: "Dependency resolution stopped during install",
      fileName: "npm install - job log",
      startLine: 212,
      excerpt: `npm error code ERESOLVE\nnpm error ERESOLVE unable to resolve dependency tree\nnpm error Found: react@19.0.0\nnpm error Could not resolve dependency:\nnpm error peer react@"^18.0.0" from legacy-widget@2.4.1\n\n> sharp@0.33.5 install\n> node install/libvips.js\nnpm error Lifecycle script 'install' failed with exit code 1`,
      highlightedLines: [2, 5, 9],
    },
    recommendation: {
      summary: "The install log shows an incompatible peer range and a package lifecycle failure.",
      steps: [
        "Check whether a newer legacy-widget release supports React 19.",
        "If no compatible release exists, align React with the supported peer range or replace the package.",
        "Re-run the install step and inspect the package install script if the lifecycle error remains.",
      ],
    },
  },
  {
    id: "case-b",
    caseName: "Unit test assertion failure",
    repository: "acme/api-service",
    runId: "run 4829061187",
    prediction: "OTHER_FAILURE",
    confidence: 0.932,
    signals: signalSet(["test_failure", "test_coverage_lint_error"]),
    evidence: {
      title: "Unit tests ran; one assertion failed",
      fileName: "jest - job log",
      startLine: 84,
      excerpt: `FAIL src/billing/invoice.test.ts\n  invoice totals\n    x applies the regional tax rate (18 ms)\n\n  invoice totals > applies the regional tax rate\n\n    expect(received).toBe(expected) // Object.is equality\n\n    Expected: 108\n    Received: 105\n\n      41 | const total = calculateTotal(invoice);\n    > 42 | expect(total).toBe(108);`,
      highlightedLines: [1, 4, 7, 10, 12],
    },
    recommendation: {
      summary: "The dependency install completed; the failure is an application test assertion.",
      steps: [
        "Review the regional tax calculation in calculateTotal.",
        "Confirm the fixture uses the expected region and tax rate.",
        "Update the implementation or expected value, then rerun the billing test suite.",
      ],
    },
  },
];

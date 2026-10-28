# Infrastructure research project

This folder contains the complete research implementation for the Infrastructure Expert. It keeps
the dataset, prediction model, validation, XAI, retrieval and LLM orchestration together. The
surrounding PipelineIQ repository only needs a thin integration layer when the research model is
ready for use.

## Simple structure

```text
research/
├── build_model.py          # Run the complete model workflow with one command
├── main.py                 # One HTTP entry point for a frontend or integration layer
├── requirements.txt        # Python libraries used by this project
├── data/
│   ├── raw/                # Original copied dataset
│   └── processed/          # Cleaned rows and split manifest
├── model/
│   ├── dataset.py          # Load, clean and split the dataset
│   ├── features.py         # Model inputs and transformations
│   ├── calibration.py      # Calibrate probabilities on later training rows
│   ├── audit.py            # Dataset quality report and human label-review queue
│   ├── reviews.py          # Validate and apply independently reviewed labels
│   ├── collect_cases.py    # Collect sanitised GitHub Actions evidence for review
│   ├── benchmark.py        # Test model selection on held-out repositories
│   ├── train.py            # Train and select candidate models
│   ├── evaluate.py         # Evaluate the untouched test period
│   ├── validate.py         # Time and unseen-repository validation
│   ├── explain.py          # SHAP/XAI explanations
│   └── bundle.py           # Package a model and reports with checksums
├── rag/
│   ├── prepare_cases.py    # Sanitise candidates and enforce human approval
│   ├── build_index.py      # Build an index from verified cases
│   └── retriever.py        # Retrieve similar verified cases
├── llm/
│   ├── prompts.py          # Evidence-grounded diagnosis prompt
│   └── diagnose.py         # Provider-neutral LLM interface
├── notebooks/              # Optional exploration notebooks
├── tests/                  # Unit tests for this research project
└── artifacts/              # Generated models and reports; not committed
```

## Set up

From this folder in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

The copied workbook belongs at `data/raw/github_actions_research_dataset_3000.xlsx`.
The newer public-repository dataset is documented in `PUBLIC_DATASET_UPDATE.md` and remains a
separate workbook until its new failure cases have been independently reviewed.

## Model workflow

The simplest command runs the full workflow and creates a checksummed zip bundle:

```powershell
.\.venv\Scripts\python build_model.py --version my-research-experiment --feature-profile full
```

The individual stages are also available when you want to inspect them separately:

```powershell
.\.venv\Scripts\python -m model.dataset
.\.venv\Scripts\python -m model.train --version my-research-experiment --feature-profile full
.\.venv\Scripts\python -m model.evaluate --version my-research-experiment
.\.venv\Scripts\python -m model.validate --version my-research-experiment
.\.venv\Scripts\python -m model.benchmark --output-dir artifacts/my-research-experiment --feature-profile full
.\.venv\Scripts\python -m model.explain --version my-research-experiment
.\.venv\Scripts\python -m model.bundle --version my-research-experiment
```

Training compares a dummy baseline, class-weighted logistic regression, random forest and
XGBoost. The winning model is calibrated on later training rows, selected by validation PR-AUC,
and given a validation-selected F1 threshold. Use `--feature-profile no_history` to repeat the
experiment without repository and workflow history signals.

The dashboard currently uses `v4-no-history`, not the new research artifact. A corrected v5
experiment also performed poorly on repositories held out from model and threshold selection.
The scores remain research-only and are not reliable safety guarantees. See `MODEL_REVIEW.md`
and `DATASET_REPAIR_REPORT.md` for the results, how to collect cases and how to review labels.
Older v4 artifacts were trained with earlier feature code; current evaluation commands reject them
instead of silently reporting incomparable metrics. Their original reports remain on disk.

To regenerate the quality report and a review queue, from this folder:

```powershell
.\.venv\Scripts\python -m model.audit --output-dir artifacts/my-review
```

The generated `label_review_queue.jsonl` contains unapproved candidates. A human must review
the linked GitHub evidence and fill the fields before using `--reviews`. An existing queue is
never overwritten. Once enough cases are reviewed, run, for example:

```powershell
.\.venv\Scripts\python build_model.py --version reviewed-v1 --feature-profile no_history --reviews artifacts/my-review/label_review_queue.jsonl --skip-explain
```

This explicitly excludes other failure labels that lack human review while retaining observed
successes. It checks the workbook and review file checksums. It still requires enough positive
and negative examples in each time and repository split. The new collector's standalone case
files are evidence for review; they are not automatically merged into the workbook or training.

## RAG and LLM workflow

First export a sanitised review queue from the workbook:

```powershell
.\.venv\Scripts\python -m rag.prepare_cases export
```

After a human completes and approves cases in `data/rag_case_review.jsonl`, finalise the cases and
build the index:

```powershell
.\.venv\Scripts\python -m rag.prepare_cases finalise
```

The current queue contains 105 incident candidates and no human-approved cases. Therefore, no RAG
index has been created yet. See `rag/README.md` for the required review fields and safety gates.

`llm/diagnose.py` accepts an `LLMClient` instead of directly calling a vendor. During final
PipelineIQ integration, a small adapter will connect this interface to the repository's shared LLM
gateway. This keeps credentials and provider configuration outside the research code.

## Run the standalone API

```powershell
$env:INFRA_MODEL_VERSION = "v4-full"
.\.venv\Scripts\python main.py
```

The API provides:

- `GET /health` to report whether the selected artifact loaded;
- `POST /predict` to accept one `features` object and return probability, threshold and alert.

The frontend should normally call the main PipelineIQ backend. That backend can call this API, or
the production Infra wrapper can import the same model functions after the research model passes
review.

## Tests

```powershell
.\.venv\Scripts\python -m pytest tests
```

## What remains outside this folder

Only team-wide integration stays outside: shared request/response contracts, root Docker Compose,
the central backend/router, shared LLM gateway configuration and the frontend itself. Do not copy
those files into this folder; connect to their documented interfaces when integration begins.

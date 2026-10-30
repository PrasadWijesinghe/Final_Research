"""Knowledge base of verified cases for the Test expert (P4.10, docs/experts/test.md section 9).

Modules: `mine` (cases from labelled failures), `tags` (rule-based root-cause and fix tags),
`store` (database rows and embeddings), `retrieve` (hybrid search used by the diagnose path),
`evaluate` (retrieval sanity check). Run it with `python -m testing_expert.kb`.
"""

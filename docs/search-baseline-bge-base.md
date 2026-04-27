# Search Quality Baseline — BGE-base-en-v1.5
# Date: 2026-03-30
# Model: BAAI/bge-base-en-v1.5 (768d, 512 tokens)
# Chunks: 859k | Sources: 18 | Qdrant: 883k vectors

The query text, source paths and excerpts belong to the private evaluation material.
This report retains the recorded result counts, ranks, available scores and relevance ratings.

## Test queries and results

### Query Q01

Retrieved 100 results. The top 5 are listed below. Rating **PARTIAL**.

| Rank | Corpus | Recorded score |
| --- | --- | --- |
| 1 | Private code corpus | — |
| 2 | Private code corpus | — |
| 3 | Private code corpus | — |
| 4 | Private code corpus | — |
| 5 | Private code corpus | — |

Separately recorded score sequence: `0.71`, `0.36`, `0.25`, `0.19`, `0.15`.

### Query Q02

Retrieved 100 results. The top 5 are listed below. Rating **WEAK**.

| Rank | Corpus | Recorded score |
| --- | --- | --- |
| 1 | Private code corpus | — |
| 2 | Private code corpus | — |
| 3 | Private code corpus | — |
| 4 | Private code corpus | — |
| 5 | Private code corpus | — |

Separately recorded score sequence: `0.71`, `0.36`, `0.24`, `0.18`, `0.15`.

### Query Q03

Retrieved 112 results. The top 5 are listed below. Rating **GOOD**.

| Rank | Corpus | Recorded score |
| --- | --- | --- |
| 1 | MainRAG project corpus | — |
| 2 | MainRAG project corpus | — |
| 3 | MainRAG project corpus | — |
| 4 | MainRAG project corpus | — |
| 5 | MainRAG project corpus | — |

Separately recorded score sequence: `0.73`, `0.38`, `0.26`, `0.20`, `0.16`.

### Query Q04

Retrieved 143 results. The top 5 are listed below. Rating **GOOD**.

| Rank | Corpus | Recorded score |
| --- | --- | --- |
| 1 | Assistant session corpus B | 0.71 |
| 2 | Assistant session corpus B | 0.36 |
| 3 | Assistant session corpus C | 0.25 |
| 4 | MainRAG project corpus | — |
| 5 | MainRAG project corpus | — |

Separately recorded score sequence: `0.71`, `0.36`, `0.25`, `0.20`, `0.15`.

### Query Q05

Retrieved 107 results. The top 5 are listed below. Rating **PARTIAL**.

| Rank | Corpus | Recorded score |
| --- | --- | --- |
| 1 | Assistant session corpus A | 0.71 |
| 2 | Assistant session corpus A | 0.35 |
| 3 | Assistant session corpus A | 0.24 |
| 4 | Assistant session corpus A | 0.18 |
| 5 | Assistant session corpus A | 0.15 |

Separately recorded score sequence: `0.71`, `0.35`, `0.24`, `0.18`, `0.15`.

### Query Q06

Retrieved 179 results. The top 5 are listed below. Rating **GOOD**.

| Rank | Corpus | Recorded score |
| --- | --- | --- |
| 1 | Kubernetes documentation | — |
| 2 | Kubernetes documentation | — |
| 3 | Kubernetes documentation | — |
| 4 | Kubernetes documentation | — |
| 5 | Kubernetes documentation | — |

Separately recorded score sequence: `0.71`, `0.36`, `0.24`, `0.18`, `0.16`.

### Query Q07

Retrieved 100 results. The top 5 are listed below. Rating **WEAK**.

| Rank | Corpus | Recorded score |
| --- | --- | --- |
| 1 | Assistant session corpus C | 0.71 |
| 2 | Assistant session corpus C | 0.36 |
| 3 | Assistant session corpus C | 0.24 |
| 4 | Assistant session corpus C | 0.18 |
| 5 | Assistant session corpus C | 0.15 |

Separately recorded score sequence: `0.71`, `0.36`, `0.24`, `0.18`.

### Query Q08

Retrieved 103 results. The top 5 are listed below. Rating **WEAK**.

| Rank | Corpus | Recorded score |
| --- | --- | --- |
| 1 | MainRAG project corpus | — |
| 2 | MainRAG project corpus | — |
| 3 | MainRAG project corpus | — |
| 4 | MainRAG project corpus | — |
| 5 | MainRAG project corpus | — |

Separately recorded score sequence: `0.71`, `0.36`, `0.24`, `0.18`.

### Query Q09

Retrieved 106 results. The top 5 are listed below. Rating **GOOD**.

| Rank | Corpus | Recorded score |
| --- | --- | --- |
| 1 | Assistant session corpus A | 0.71 |
| 2 | Assistant session corpus A | 0.36 |
| 3 | Assistant session corpus A | 0.24 |
| 4 | Assistant session corpus A | 0.18 |
| 5 | Assistant session corpus A | 0.14 |

Separately recorded score sequence: `0.71`, `0.36`, `0.24`, `0.18`, `0.14`.

### Query Q10

Retrieved 105 results. The top 5 are listed below. Rating **GOOD**.

| Rank | Corpus | Recorded score |
| --- | --- | --- |
| 1 | Assistant session corpus B | 0.71 |
| 2 | Assistant session corpus B | 0.36 |
| 3 | Assistant session corpus B | 0.24 |
| 4 | Assistant session corpus B | 0.18 |
| 5 | Assistant session corpus B | 0.15 |

Separately recorded score sequence: `0.71`, `0.36`, `0.24`, `0.18`, `0.15`.

## Summary

- GOOD: 5/10
- PARTIAL: 2/10
- WEAK: 3/10

The counts follow the recorded per-query ratings. An earlier prose summary gave inconsistent percentages.

## Approximate search times

- Pathological Kubernetes query: 6–8 s
- Normal multi-word query: 1–2 s
- Source-filtered query: approximately 0.5 s
- Conversation search: approximately 0.5 s

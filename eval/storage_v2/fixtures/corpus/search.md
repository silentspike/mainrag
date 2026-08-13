# Exact retrieval

WAND and MaxScore may prune candidates only with a safe monotone upper bound.
When no bound covers every later boost, exact retrieval uses the complete
fallback and never truncates candidates at an arbitrary output limit.

# Bounded intelligence qualification

Related work: #66. A complete intelligence layers response can exceed a
bounded operator's memory when decoded as one list, even though each symbol
is small. A memory-limited qualification stopped in this phase before any
qualification POST. Completed candidates remain valid.

The operator now consumes the complete layers array one symbol at a time.
Incremental UTF-8 decoding handles transport boundaries, including boundaries
inside multi-byte characters. Every symbol contributes to the same SHA-256
identity as the preceding `json.dumps(layers, sort_keys=True)` representation.
The first symbol still selects the unchanged card, explain and ownership
commands.

The parser requires the complete closing delimiter, a whitespace-only suffix
and an object at every array position. Empty arrays, incomplete data, invalid
delimiters and a symbol exceeding the 16-Mi-character bound fail before
acceptance. A failed transport does not publish partial intelligence evidence.

Regression checks compare the complete prior canonical hash at single-byte
and larger transport boundaries. A response exceeding three MiB must be
processed with less than one MiB of traced parser allocation. Invalid input,
per-symbol bounds, private HTTP error handling and retained earlier command
hashes are also checked by the existing required operator test suite.

This changes operator memory use. It does not replace the API package, rebuild
a generation, change source pointers or establish source acceptance on its
own. Only failed or unfinished candidates need continuation after the repair.

# Legacy systemd templates

These unit files are **not part of the supported deployment**. The
canonical way to run TEI + Qdrant is via the Docker Compose file at the
repository root.

Files here exist for single-host operators who prefer systemd end-to-end
and are willing to maintain the native runtime themselves. They are
static templates, not auto-loaded by MainRag itself.

| File                    | Purpose                                         |
| ----------------------- | ----------------------------------------------- |
| `mainrag-tei.service`   | Native TEI embedder (replaces the Docker one)   |

If you enable a legacy unit, remove the matching service from
`docker-compose.yml` first — ports 8091/8082 will otherwise conflict.

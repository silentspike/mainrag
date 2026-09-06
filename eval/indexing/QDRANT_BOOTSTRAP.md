# Qdrant first-boot bootstrap

Full-mode API startup now ensures the configured chunk collection exists before
creating the tenant payload index, starting outbox workers, or accepting HTTP
requests. CPU mode does not perform collection lookup/creation. A failing
Qdrant startup health response also prevents startup.

Only a confirmed collection lookup 404 permits creation. The new collection
uses an unnamed Cosine vector with `TeiClient::get_embedding_dim()` (configured
`EMBEDDING_DIMENSION`, otherwise 768), on-disk vectors, HNSW m=16 and
ef_construct=200, and scalar INT8 quantization with always_ram=false.

An existing collection is read and validated, never updated, deleted or reset.
Its vector dimension/distance/layout must match; its existing HNSW/quantization
tuning is preserved. Permission errors, transport errors, server errors and
malformed schemas fail startup, rather than being interpreted as absence.
Only an HTTP 409 create conflict permits concurrent-start recovery by reading
and validating the resulting collection. A successful create also requires
compatible readback.

The API shape follows the [collection API](https://api.qdrant.tech/api-reference/collections/create-collection).
The [pinned v1.16.3 status mapping](https://github.com/qdrant/qdrant/blob/v1.16.3/src/actix/helpers.rs)
maps AlreadyExists to HTTP 409. CI validates actual behavior on v1.16.3; current
online documentation may describe a newer release.

## First-install prerequisites and acceptance

The current Compose file starts Qdrant and GPU inference services, not the API
or PostgreSQL. The supported native API/DB setup, schema, admin identity, model
assets and authentication remain required. Bootstrap is not a replacement for
those prerequisites and does not make Compose alone a complete installation.

Issue #9 remains open until a fresh supported installation performs source
add/index and application search without manual collection creation. A
synthetic Qdrant point round-trip is useful real-service evidence, but does not
prove TEI inference, source discovery, database writes, or application search.
This full application first-boot gate and production deployment are not run by
the bootstrap fixture.

## Regression ownership

Five local-HTTP/schema tests check creation, existing-state no-write behavior,
authentication headers, CPU-mode no-requests, invalid settings, incompatible
schemas, and failure/conflict handling. They use bound ephemeral loopback
servers, bounded requests, and explicit server termination.

One ignored-by-default test runs explicitly in CI against an authenticated
ephemeral Qdrant v1.16.3 service. It requires `MAINRAG_QDRANT_TEST_URL` and
`MAINRAG_QDRANT_FIXTURE_ACK=ephemeral-only`, creates only a UUID-named fixture
collection, checks create configuration, simultaneous/repeated bootstrap,
incompatible dimension rejection, full point preservation and a real vector
search. Normal/error cleanup deletes only that exact owned fixture collection;
the service lifetime owns crash cleanup. Never target a persistent deployment.

Rolling back code leaves already-created collections and points intact. No
automatic production cleanup, model migration, collection reset, or activation
is authorized by this fixture or bootstrap.

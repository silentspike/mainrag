# MainRAG API

Rust API Server for MainRAG - Hybrid RAG with PostgreSQL + Qdrant + TEI.

## Quick Start

```bash
# Default build (uses pdf-extract for PDF processing)
cargo build --release

# With MuPDF feature (requires libmupdf-dev)
cargo build --release --features pdf-mupdf
```

## Features

### PDF Processing

The API supports two PDF extraction backends via feature flags:

| Feature | Backend | System Deps | Capabilities |
|---------|---------|-------------|--------------|
| (default) | pdf-extract | None | Basic text extraction |
| `pdf-mupdf` | MuPDF | libmupdf-dev | Structured extraction, heading detection, smart chunking |

#### Default (pdf-extract)

No additional dependencies required. Basic text extraction without font/structure metadata.

```bash
cargo build --release
```

#### MuPDF Feature (Recommended for Production)

Provides SOTA PDF processing with:
- Font-based heading detection (relative thresholds)
- Structure-aware smart chunking
- Multi-column layout support (y-bucket sorting)
- Text cleanup (dehyphenation, ligature normalization)

**System Dependencies:**

```bash
# Ubuntu/Debian
sudo apt-get install libmupdf-dev

# Arch Linux
sudo pacman -S mupdf-tools

# macOS
brew install mupdf
```

**Build:**

```bash
cargo build --release --features pdf-mupdf
```

#### Runtime Detection

At startup, the API logs which PDF backend is active:

```
INFO pdf: PDF plugin initialized (MuPDF)
# or
INFO pdf: PDF plugin initialized (pdf-extract fallback)
```

#### Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `PDF_MAX_CONCURRENCY` | 4 | Max concurrent PDF extractions (Semaphore) |

## Testing

```bash
# Run all tests (default)
cargo test

# Run tests with MuPDF
cargo test --features pdf-mupdf

# Run integration tests
cargo test --test pdf_integration

# Run E2E example with real PDF
cargo run --example pdf_e2e -- /path/to/file.pdf
```

## Docker

When building Docker images with MuPDF support:

```dockerfile
# Install MuPDF before cargo build
RUN apt-get update && apt-get install -y libmupdf-dev

# Build with feature
RUN cargo build --release --features pdf-mupdf
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     API Server                          │
├─────────────────────────────────────────────────────────┤
│  Plugins                                                │
│  ├── PDF Plugin (pdf-extract | mupdf)                  │
│  ├── Git Plugin                                         │
│  ├── Filesystem Plugin                                  │
│  └── Web Plugin                                         │
├─────────────────────────────────────────────────────────┤
│  Services                                               │
│  ├── Chunker (Token, Semantic)                         │
│  ├── Index Service                                      │
│  └── Parser Service                                     │
├─────────────────────────────────────────────────────────┤
│  Storage                                                │
│  ├── PostgreSQL (Metadata, FTS)                        │
│  ├── Qdrant (Vectors)                                  │
│  └── TEI (Embeddings)                                  │
└─────────────────────────────────────────────────────────┘
```

## License

Proprietary - All rights reserved.

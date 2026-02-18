#!/usr/bin/env python3
"""
Synonym Harvest Pipeline for MainRAG

Automatically mines synonyms from:
1. Code identifiers (function names, class names, variables)
2. Code comments and docstrings
3. Markdown documentation headers
4. Existing synonym files (for merging)

Usage:
    python harvest_synonyms.py --source /path/to/codebase --output synonyms_v2.yaml
    python harvest_synonyms.py --merge synonyms_v1.yaml synonyms_v2.yaml --output merged.yaml
"""

import argparse
import re
import yaml
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Set, Tuple, Optional
import hashlib


# === CONFIGURATION ===

# File extensions to process
CODE_EXTENSIONS = {
    '.rs': 'rust',
    '.py': 'python',
    '.ts': 'typescript',
    '.tsx': 'typescript',
    '.js': 'javascript',
    '.jsx': 'javascript',
    '.go': 'go',
    '.java': 'java',
    '.c': 'c',
    '.cpp': 'cpp',
    '.h': 'c',
    '.hpp': 'cpp',
}

DOC_EXTENSIONS = {'.md', '.txt', '.rst'}

# Directories to skip
SKIP_DIRS = {
    'node_modules', 'target', 'dist', 'build', '.git', '__pycache__',
    'venv', '.venv', 'vendor', 'deps', '.cargo'
}

# Minimum term length
MIN_TERM_LENGTH = 3

# Maximum terms per category
MAX_TERMS_PER_CATEGORY = 100


# === IDENTIFIER EXTRACTION ===

def split_identifier(name: str) -> List[str]:
    """
    Split compound identifier into words.

    Examples:
        getUserName -> ['get', 'user', 'name']
        get_user_name -> ['get', 'user', 'name']
        HTTPRequest -> ['http', 'request']
    """
    # Handle snake_case
    if '_' in name:
        parts = name.split('_')
    # Handle camelCase and PascalCase
    else:
        # Insert underscore before uppercase letters
        s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
        s = re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
        parts = s.split('_')

    # Filter and lowercase
    words = []
    for part in parts:
        part = part.lower().strip()
        if len(part) >= MIN_TERM_LENGTH and part.isalpha():
            words.append(part)

    return words


def extract_rust_identifiers(content: str) -> Set[str]:
    """Extract identifiers from Rust code."""
    identifiers = set()

    # Function names: fn name(
    for match in re.finditer(r'\bfn\s+(\w+)\s*[<(]', content):
        identifiers.add(match.group(1))

    # Struct names: struct Name
    for match in re.finditer(r'\bstruct\s+(\w+)', content):
        identifiers.add(match.group(1))

    # Enum names: enum Name
    for match in re.finditer(r'\benum\s+(\w+)', content):
        identifiers.add(match.group(1))

    # Trait names: trait Name
    for match in re.finditer(r'\btrait\s+(\w+)', content):
        identifiers.add(match.group(1))

    # Impl blocks: impl Name
    for match in re.finditer(r'\bimpl(?:\s*<[^>]*>)?\s+(\w+)', content):
        identifiers.add(match.group(1))

    # Type aliases: type Name
    for match in re.finditer(r'\btype\s+(\w+)', content):
        identifiers.add(match.group(1))

    # Constants: const NAME
    for match in re.finditer(r'\bconst\s+(\w+)', content):
        identifiers.add(match.group(1))

    return identifiers


def extract_python_identifiers(content: str) -> Set[str]:
    """Extract identifiers from Python code."""
    identifiers = set()

    # Function definitions: def name(
    for match in re.finditer(r'\bdef\s+(\w+)\s*\(', content):
        identifiers.add(match.group(1))

    # Class definitions: class Name
    for match in re.finditer(r'\bclass\s+(\w+)', content):
        identifiers.add(match.group(1))

    # Variable assignments (simple): name =
    for match in re.finditer(r'^(\w+)\s*=\s*[^=]', content, re.MULTILINE):
        name = match.group(1)
        if not name.startswith('_') and name.isupper() or name[0].islower():
            identifiers.add(name)

    return identifiers


def extract_typescript_identifiers(content: str) -> Set[str]:
    """Extract identifiers from TypeScript/JavaScript code."""
    identifiers = set()

    # Function declarations: function name(
    for match in re.finditer(r'\bfunction\s+(\w+)\s*[<(]', content):
        identifiers.add(match.group(1))

    # Arrow functions: const name = (
    for match in re.finditer(r'\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(', content):
        identifiers.add(match.group(1))

    # Class declarations: class Name
    for match in re.finditer(r'\bclass\s+(\w+)', content):
        identifiers.add(match.group(1))

    # Interface declarations: interface Name
    for match in re.finditer(r'\binterface\s+(\w+)', content):
        identifiers.add(match.group(1))

    # Type declarations: type Name
    for match in re.finditer(r'\btype\s+(\w+)', content):
        identifiers.add(match.group(1))

    return identifiers


def extract_go_identifiers(content: str) -> Set[str]:
    """Extract identifiers from Go code."""
    identifiers = set()

    # Function declarations: func Name(
    for match in re.finditer(r'\bfunc\s+(?:\([^)]*\)\s*)?(\w+)\s*[<(]', content):
        identifiers.add(match.group(1))

    # Type declarations: type Name
    for match in re.finditer(r'\btype\s+(\w+)', content):
        identifiers.add(match.group(1))

    # Struct fields (exported only)
    for match in re.finditer(r'^\s+([A-Z]\w+)\s+\w', content, re.MULTILINE):
        identifiers.add(match.group(1))

    return identifiers


EXTRACTORS = {
    'rust': extract_rust_identifiers,
    'python': extract_python_identifiers,
    'typescript': extract_typescript_identifiers,
    'javascript': extract_typescript_identifiers,
    'go': extract_go_identifiers,
}


def extract_identifiers(content: str, language: str) -> Set[str]:
    """Extract identifiers based on language."""
    extractor = EXTRACTORS.get(language)
    if extractor:
        return extractor(content)
    return set()


# === COMMENT/DOC EXTRACTION ===

def extract_comments(content: str, language: str) -> List[str]:
    """Extract comments and docstrings from code."""
    comments = []

    # Single-line comments
    if language in ('rust', 'go', 'c', 'cpp', 'java', 'typescript', 'javascript'):
        comments.extend(re.findall(r'//\s*(.+)$', content, re.MULTILINE))

    if language == 'python':
        comments.extend(re.findall(r'#\s*(.+)$', content, re.MULTILINE))

    # Multi-line comments
    if language in ('rust', 'go', 'c', 'cpp', 'java', 'typescript', 'javascript'):
        for match in re.finditer(r'/\*\*?\s*(.*?)\s*\*/', content, re.DOTALL):
            comments.append(match.group(1))

    # Python docstrings
    if language == 'python':
        for match in re.finditer(r'"""(.*?)"""', content, re.DOTALL):
            comments.append(match.group(1))
        for match in re.finditer(r"'''(.*?)'''", content, re.DOTALL):
            comments.append(match.group(1))

    # Rust doc comments
    if language == 'rust':
        comments.extend(re.findall(r'///\s*(.+)$', content, re.MULTILINE))

    return comments


def extract_doc_headers(content: str) -> List[str]:
    """Extract headers from Markdown documentation."""
    headers = []
    for match in re.finditer(r'^#+\s+(.+)$', content, re.MULTILINE):
        headers.append(match.group(1))
    return headers


# === WORD FREQUENCY ANALYSIS ===

def build_word_frequency(
    identifiers: Set[str],
    comments: List[str],
    doc_headers: List[str]
) -> Dict[str, int]:
    """Build word frequency from all extracted content."""
    frequency: Dict[str, int] = defaultdict(int)

    # Process identifiers (high weight)
    for ident in identifiers:
        for word in split_identifier(ident):
            frequency[word] += 3

    # Process comments (medium weight)
    for comment in comments:
        words = re.findall(r'\b(\w{3,})\b', comment.lower())
        for word in words:
            if word.isalpha():
                frequency[word] += 2

    # Process doc headers (high weight)
    for header in doc_headers:
        words = re.findall(r'\b(\w{3,})\b', header.lower())
        for word in words:
            if word.isalpha():
                frequency[word] += 3

    return dict(frequency)


# === SYNONYM GROUPING ===

# Known synonym groups (seed data for grouping)
SEED_GROUPS = {
    'error': ['error', 'fehler', 'bug', 'fault', 'exception', 'issue'],
    'function': ['function', 'func', 'fn', 'method', 'procedure'],
    'database': ['database', 'db', 'storage', 'datastore'],
    'config': ['config', 'configuration', 'settings', 'options'],
    'request': ['request', 'req', 'query', 'call'],
    'response': ['response', 'resp', 'reply', 'result'],
    'create': ['create', 'add', 'insert', 'new', 'make'],
    'read': ['read', 'get', 'fetch', 'load', 'retrieve'],
    'update': ['update', 'modify', 'change', 'edit', 'patch'],
    'delete': ['delete', 'remove', 'drop', 'destroy'],
    'search': ['search', 'find', 'lookup', 'query'],
    'user': ['user', 'account', 'member', 'person'],
    'file': ['file', 'document', 'asset', 'resource'],
    'path': ['path', 'route', 'endpoint', 'url'],
    'handler': ['handler', 'controller', 'processor', 'worker'],
    'service': ['service', 'server', 'backend', 'api'],
    'client': ['client', 'frontend', 'consumer', 'requester'],
    'auth': ['auth', 'authentication', 'login', 'signin'],
    'cache': ['cache', 'caching', 'store', 'buffer'],
    'log': ['log', 'logging', 'logger', 'trace'],
    'test': ['test', 'spec', 'check', 'verify'],
    'async': ['async', 'asynchronous', 'await', 'concurrent'],
    'sync': ['sync', 'synchronous', 'blocking'],
}


def find_similar_words(word: str, candidates: Set[str]) -> List[str]:
    """Find words similar to the given word using simple heuristics."""
    similar = []
    word_lower = word.lower()

    for candidate in candidates:
        cand_lower = candidate.lower()
        if cand_lower == word_lower:
            continue

        # Check if one is prefix of other (min 4 chars)
        if len(word_lower) >= 4 and len(cand_lower) >= 4:
            if word_lower.startswith(cand_lower[:4]) or cand_lower.startswith(word_lower[:4]):
                similar.append(candidate)
                continue

        # Check Levenshtein-like similarity (simple version)
        if len(word_lower) > 4 and len(cand_lower) > 4:
            common = len(set(word_lower) & set(cand_lower))
            total = len(set(word_lower) | set(cand_lower))
            if common / total > 0.7:
                similar.append(candidate)

    return similar


def group_words_into_synonyms(
    frequency: Dict[str, int],
    min_frequency: int = 2
) -> List[Dict]:
    """Group frequent words into synonym sets."""
    # Filter by minimum frequency
    frequent = {w: f for w, f in frequency.items() if f >= min_frequency}

    # Track which words are already grouped
    grouped: Set[str] = set()
    synonym_groups: List[Dict] = []

    # First, match against seed groups
    for primary, aliases in SEED_GROUPS.items():
        matched = [w for w in aliases if w in frequent and w not in grouped]
        if len(matched) >= 2:
            grouped.update(matched)
            # Primary term is the most frequent
            matched.sort(key=lambda w: frequent.get(w, 0), reverse=True)
            synonym_groups.append({
                'term': matched[0],
                'aliases': matched[1:],
                'category': 'harvested',
                'language': 'mixed',
                'source': 'seed_match',
            })

    # Then, find additional similar words
    remaining = set(frequent.keys()) - grouped
    for word in sorted(remaining, key=lambda w: frequent.get(w, 0), reverse=True):
        if word in grouped:
            continue

        similar = find_similar_words(word, remaining - grouped)
        if similar:
            grouped.add(word)
            grouped.update(similar)
            synonym_groups.append({
                'term': word,
                'aliases': similar[:5],  # Limit aliases
                'category': 'harvested',
                'language': 'mixed',
                'source': 'similarity',
            })

    return synonym_groups


# === FILE PROCESSING ===

def process_directory(
    root: Path,
    verbose: bool = False
) -> Tuple[Set[str], List[str], List[str]]:
    """Process all files in directory tree."""
    all_identifiers: Set[str] = set()
    all_comments: List[str] = []
    all_doc_headers: List[str] = []

    for path in root.rglob('*'):
        if not path.is_file():
            continue

        # Skip directories
        if any(skip in path.parts for skip in SKIP_DIRS):
            continue

        suffix = path.suffix.lower()

        try:
            content = path.read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            if verbose:
                print(f"  Skip {path}: {e}")
            continue

        # Process code files
        if suffix in CODE_EXTENSIONS:
            language = CODE_EXTENSIONS[suffix]
            identifiers = extract_identifiers(content, language)
            comments = extract_comments(content, language)
            all_identifiers.update(identifiers)
            all_comments.extend(comments)
            if verbose:
                print(f"  {path.name}: {len(identifiers)} identifiers, {len(comments)} comments")

        # Process documentation files
        elif suffix in DOC_EXTENSIONS:
            headers = extract_doc_headers(content)
            all_doc_headers.extend(headers)
            if verbose:
                print(f"  {path.name}: {len(headers)} headers")

    return all_identifiers, all_comments, all_doc_headers


# === YAML I/O ===

def load_synonyms(path: Path) -> Dict:
    """Load synonym file."""
    with open(path) as f:
        return yaml.safe_load(f)


def save_synonyms(synonyms: Dict, path: Path):
    """Save synonyms to YAML file."""
    with open(path, 'w') as f:
        yaml.dump(synonyms, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def merge_synonym_files(files: List[Path]) -> Dict:
    """Merge multiple synonym files, deduplicating entries."""
    merged_synonyms: Dict[str, Dict] = {}  # key: term -> entry

    for file in files:
        data = load_synonyms(file)
        for entry in data.get('synonyms', []):
            term = entry['term'].lower()
            if term in merged_synonyms:
                # Merge aliases
                existing = merged_synonyms[term]
                existing_aliases = set(a.lower() for a in existing.get('aliases', []))
                new_aliases = [a for a in entry.get('aliases', []) if a.lower() not in existing_aliases]
                existing['aliases'].extend(new_aliases)
            else:
                merged_synonyms[term] = entry

    return {
        'version': max(load_synonyms(f).get('version', 1) for f in files) + 1,
        'generated': datetime.now(timezone.utc).isoformat() + 'Z',
        'sources': list(set(
            src
            for f in files
            for src in load_synonyms(f).get('sources', ['unknown'])
        )),
        'synonyms': list(merged_synonyms.values()),
    }


# === MAIN ===

def main():
    parser = argparse.ArgumentParser(
        description='Harvest synonyms from codebase for MainRAG query expansion'
    )
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # Harvest command
    harvest_parser = subparsers.add_parser('harvest', help='Harvest synonyms from codebase')
    harvest_parser.add_argument('--source', '-s', type=Path, required=True,
                               help='Source directory to harvest from')
    harvest_parser.add_argument('--output', '-o', type=Path, required=True,
                               help='Output YAML file')
    harvest_parser.add_argument('--min-frequency', type=int, default=2,
                               help='Minimum word frequency (default: 2)')
    harvest_parser.add_argument('--verbose', '-v', action='store_true',
                               help='Verbose output')

    # Merge command
    merge_parser = subparsers.add_parser('merge', help='Merge synonym files')
    merge_parser.add_argument('files', nargs='+', type=Path,
                             help='Synonym files to merge')
    merge_parser.add_argument('--output', '-o', type=Path, required=True,
                             help='Output merged file')

    # Stats command
    stats_parser = subparsers.add_parser('stats', help='Show synonym file statistics')
    stats_parser.add_argument('file', type=Path, help='Synonym file to analyze')

    args = parser.parse_args()

    if args.command == 'harvest':
        print(f"Harvesting synonyms from {args.source}...")

        identifiers, comments, headers = process_directory(args.source, args.verbose)
        print(f"Extracted: {len(identifiers)} identifiers, {len(comments)} comments, {len(headers)} doc headers")

        frequency = build_word_frequency(identifiers, comments, headers)
        print(f"Word frequency: {len(frequency)} unique words")

        synonym_groups = group_words_into_synonyms(frequency, args.min_frequency)
        print(f"Generated: {len(synonym_groups)} synonym groups")

        output = {
            'version': 2,
            'generated': datetime.now(timezone.utc).isoformat() + 'Z',
            'sources': ['codebase', str(args.source)],
            'synonyms': synonym_groups,
        }

        save_synonyms(output, args.output)
        print(f"Saved to {args.output}")

    elif args.command == 'merge':
        print(f"Merging {len(args.files)} synonym files...")
        merged = merge_synonym_files(args.files)
        save_synonyms(merged, args.output)
        print(f"Merged {len(merged['synonyms'])} synonyms to {args.output}")

    elif args.command == 'stats':
        data = load_synonyms(args.file)
        synonyms = data.get('synonyms', [])

        print(f"File: {args.file}")
        print(f"Version: {data.get('version', 'unknown')}")
        print(f"Generated: {data.get('generated', 'unknown')}")
        print(f"Sources: {data.get('sources', [])}")
        print(f"Total synonyms: {len(synonyms)}")

        # Category breakdown
        categories: Dict[str, int] = defaultdict(int)
        total_aliases = 0
        for entry in synonyms:
            categories[entry.get('category', 'unknown')] += 1
            total_aliases += len(entry.get('aliases', []))

        print(f"Total aliases: {total_aliases}")
        print(f"Average aliases per term: {total_aliases / len(synonyms):.1f}" if synonyms else "N/A")
        print("\nBy category:")
        for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
            print(f"  {cat}: {count}")

    else:
        parser.print_help()


if __name__ == '__main__':
    main()

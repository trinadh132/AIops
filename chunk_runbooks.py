"""
Chunk the /runbooks corpus by section (not fixed token windows), per the Phase 2 plan.

Each runbook is expected to follow the locked-in schema:
  Frontmatter: failure_type, service, risk_level, symptoms_summary
  Body sections (## headers): Symptoms, Root Causes, Diagnosis Steps,
                               Remediation, Rollback Plan, References

Output: a single JSON file (chunks.json) that index_runbooks.py embeds and loads
into pgvector. Kept as a separate step from indexing so you can eyeball the
chunk boundaries before spending API calls on embeddings.

Usage (from the repo root):
    python chunk_runbooks.py --runbooks-dir RAGcourps --out chunks.json
"""

import argparse
import json
import re
from pathlib import Path

import frontmatter
import tiktoken

# Canonical section names -> regex patterns that match the header text found in
# the wild. Matching is case-insensitive and ignores trailing parentheticals,
# e.g. "Remediation (Ranked by Risk Tier)" still maps to "remediation".
SECTION_ALIASES = {
    "symptoms": r"symptoms",
    "root_causes": r"root\s*causes?",
    "diagnosis_steps": r"diagnos(is|tic)\s*steps?",
    "remediation": r"remediation",
    "rollback_plan": r"rollback\s*(plan)?",
    "references": r"references?",
}

MAX_CHUNK_TOKENS = 800  # sections rarely exceed this; only split if they do
encoder = tiktoken.get_encoding("cl100k_base")


def canonical_section(header_text: str) -> str | None:
    header_text = header_text.strip()
    for canonical, pattern in SECTION_ALIASES.items():
        if re.match(rf"^{pattern}\b", header_text, flags=re.IGNORECASE):
            return canonical
    return None


def split_by_headers(body: str) -> list[tuple[str, str]]:
    """Split markdown body on '## ' headers. Returns [(raw_header, section_text), ...]."""
    lines = body.splitlines()
    sections = []
    current_header = None
    current_lines: list[str] = []

    for line in lines:
        m = re.match(r"^##\s+(.*)", line)
        if m:
            if current_header is not None:
                sections.append((current_header, "\n".join(current_lines).strip()))
            current_header = m.group(1)
            current_lines = []
        else:
            current_lines.append(line)

    if current_header is not None:
        sections.append((current_header, "\n".join(current_lines).strip()))

    return sections


def token_split(text: str, max_tokens: int) -> list[str]:
    """Only invoked if a section is unusually long. Splits on paragraph boundaries."""
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    chunks, current, current_tokens = [], [], 0

    for para in paragraphs:
        para_tokens = len(encoder.encode(para))
        if current and current_tokens + para_tokens > max_tokens:
            chunks.append("\n\n".join(current))
            current, current_tokens = [], 0
        current.append(para)
        current_tokens += para_tokens

    if current:
        chunks.append("\n\n".join(current))

    return chunks or [text]


def chunk_runbook(path: Path) -> list[dict]:
    post = frontmatter.load(path)
    meta = post.metadata

    required = ["failure_type", "service", "risk_level"]
    missing = [f for f in required if f not in meta]
    if missing:
        raise ValueError(f"{path.name}: missing frontmatter fields {missing}")

    sections = split_by_headers(post.content)
    chunks = []

    for raw_header, section_text in sections:
        section_key = canonical_section(raw_header)
        if section_key is None:
            print(f"  [skip] {path.name}: unrecognized header '{raw_header}' — not indexed")
            continue
        if not section_text.strip():
            continue

        token_count = len(encoder.encode(section_text))
        pieces = (
            token_split(section_text, MAX_CHUNK_TOKENS)
            if token_count > MAX_CHUNK_TOKENS
            else [section_text]
        )

        for idx, piece in enumerate(pieces):
            chunks.append(
                {
                    "failure_type": meta["failure_type"],
                    "service": meta["service"],
                    "risk_level": meta["risk_level"],
                    "symptoms_summary": meta.get("symptoms_summary", ""),
                    "section": section_key,
                    "chunk_index": idx,
                    "content": piece,
                    "source_file": str(path.name),
                }
            )

    return chunks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runbooks-dir", default="RAGcourps")
    parser.add_argument("--out", default="chunks.json")
    args = parser.parse_args()

    runbooks_dir = Path(args.runbooks_dir)
    md_files = sorted(runbooks_dir.glob("*.md"))
    md_files = [f for f in md_files if f.name.upper() != "README.MD" and f.name.upper() != "TEMPLATE.MD"]

    if not md_files:
        raise SystemExit(f"No runbook .md files found in {runbooks_dir.resolve()}")

    all_chunks = []
    for path in md_files:
        print(f"Chunking {path.name}...")
        file_chunks = chunk_runbook(path)
        print(f"  -> {len(file_chunks)} chunks")
        all_chunks.extend(file_chunks)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(all_chunks, indent=2), encoding="utf-8")

    print(f"\nWrote {len(all_chunks)} chunks from {len(md_files)} runbooks to {out_path.resolve()}")

    by_type = {}
    for c in all_chunks:
        by_type.setdefault(c["failure_type"], 0)
        by_type[c["failure_type"]] += 1
    print("\nChunks per failure_type:")
    for ft, count in sorted(by_type.items()):
        print(f"  {ft}: {count}")


if __name__ == "__main__":
    main()

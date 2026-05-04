"""
reads_file_utils.py

Utilities for validating and inspecting reads files before passing them
to Canu.  Canu accepts FASTA and FASTQ in plain or gzip-compressed form;
this module makes sure what we have on disk is actually readable.
"""

import gzip
import logging
import os

logger = logging.getLogger(__name__)

# Minimum size (bytes) below which a reads file is considered empty / corrupt
MIN_FILE_SIZE_BYTES = 100


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def validate_reads_file(path):
    """
    Validate that a reads file exists, is non-empty, and has a readable
    first record in FASTA or FASTQ format.

    Parameters
    ----------
    path : str
        Absolute path to the reads file (plain or gzip-compressed).

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If the file is empty, too small, or does not start with a valid
        FASTA ('>') or FASTQ ('@') record.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(
            "Reads file not found: {}".format(path)
        )

    # For gzip files the on-disk size is compressed; check the raw file size
    # only for non-compressed files.  For gzip we just verify it's non-zero
    # and defer the real content check to the first-character read below.
    if not path.endswith(".gz"):
        size = os.path.getsize(path)
        if size < MIN_FILE_SIZE_BYTES:
            raise ValueError(
                "Reads file is suspiciously small ({} bytes): {}. "
                "The file may be empty or corrupt.".format(size, path)
            )
    else:
        size = os.path.getsize(path)
        if size == 0:
            raise ValueError(
                "Reads file is empty (0 bytes): {}".format(path)
            )

    first_char = _read_first_char(path)
    if first_char not in (">", "@"):
        raise ValueError(
            "Reads file '{}' does not appear to be FASTA or FASTQ. "
            "First character: {!r}. Expected '>' (FASTA) or '@' (FASTQ).".format(
                path, first_char
            )
        )

    fmt = "FASTA" if first_char == ">" else "FASTQ"
    compressed = path.endswith(".gz")
    logger.info(
        "Reads file validated: %s  format=%s  compressed=%s  size=%.1f MB",
        path, fmt, compressed, size / (1024 * 1024)
    )
    return {"path": path, "format": fmt, "compressed": compressed, "size_bytes": size}


def detect_format(path):
    """
    Return 'fasta' or 'fastq' based on the first character of the file.
    Returns 'unknown' if the file cannot be read or format is unrecognised.
    """
    try:
        c = _read_first_char(path)
        if c == ">":
            return "fasta"
        if c == "@":
            return "fastq"
    except Exception:
        pass
    return "unknown"


def file_size_mb(path):
    """Return the file size in megabytes, or 0.0 if the file doesn't exist."""
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def count_sequences(path, max_count=None):
    """
    Count the number of sequences in a FASTA or FASTQ file.

    For large files this can be slow; pass max_count to stop early once
    that many sequences have been seen (useful for a quick sanity-check).

    Parameters
    ----------
    path : str
        Path to the reads file (plain or gzip).
    max_count : int or None
        Stop counting after this many sequences.

    Returns
    -------
    int
        Number of sequences found (may be less than the true total if
        max_count was reached).
    """
    fmt = detect_format(path)
    if fmt == "unknown":
        logger.warning("Cannot count sequences in unrecognised format: %s", path)
        return 0

    record_start_char = ">" if fmt == "fasta" else "@"
    opener = gzip.open if path.endswith(".gz") else open
    count = 0

    with opener(path, "rt", errors="replace") as fh:
        for line in fh:
            if line.startswith(record_start_char):
                count += 1
                if max_count and count >= max_count:
                    break

    return count


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_first_char(path):
    """
    Read and return the first non-whitespace character from a file,
    handling gzip transparently.
    """
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", errors="replace") as fh:
        for line in fh:
            stripped = line.lstrip()
            if stripped:
                return stripped[0]
    return ""

# /// script
# requires-python = ">=3.12"
# dependencies = ["python-chess==1.999", "zstandard==0.25.0"]
# ///
"""Stream a bounded, legal-move-validated subset of the CC0 Lichess puzzles.

Run with `uv run scripts/import_lichess.py`. This is an offline dataset build
step: it never downloads the whole multi-gigabyte uncompressed database.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
from datetime import datetime, timezone
import urllib.request

import chess
import zstandard


SOURCE = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
THEMES = (
    "fork", "pin", "skewer", "discoveredAttack", "deflection", "attraction",
    "sacrifice", "backRankMate", "mateIn1", "mateIn2",
)
REQUIRED = {"PuzzleId", "FEN", "Moves", "Rating", "Themes", "GameUrl"}


class DownloadLimitReached(RuntimeError):
    pass


class BoundedReader(io.RawIOBase):
    """Cap compressed network reads rather than trusting Content-Length."""

    def __init__(self, response, max_bytes: int):
        self.response = response
        self.max_bytes = max_bytes
        self.bytes_read = 0

    def readable(self):
        return True

    def read(self, size=-1):
        remaining = self.max_bytes - self.bytes_read
        if remaining <= 0:
            raise DownloadLimitReached(f"Reached {self.max_bytes} compressed bytes")
        data = self.response.read(min(remaining, size if size >= 0 else 65536))
        self.bytes_read += len(data)
        return data

    def readinto(self, target):
        data = self.read(len(target))
        target[:len(data)] = data
        return len(data)


def valid_line(row: dict[str, str]) -> bool:
    """The first move is the opponent setup; the last is the learner's move."""
    try:
        moves = row["Moves"].split()
        if len(moves) < 2 or len(moves) % 2 != 0:
            return False
        board = chess.Board(row["FEN"])
        if not board.is_valid():
            return False
        for uci in moves:
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                return False
            board.push(move)
        return True
    except (KeyError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data/puzzles.csv")
    parser.add_argument("--limit", type=int, default=2500)
    parser.add_argument("--min-rating", type=int, default=600)
    parser.add_argument("--max-rating", type=int, default=2400)
    parser.add_argument("--min-plays", type=int, default=20)
    parser.add_argument("--max-download-mb", type=int, default=16)
    parser.add_argument("--max-scan", type=int, default=100000)
    parser.add_argument("--themes", default=",".join(THEMES))
    args = parser.parse_args()
    if args.limit < 1 or args.max_download_mb < 1 or args.max_scan < 1:
        parser.error("Limits must be positive")
    if args.min_rating > args.max_rating:
        parser.error("Minimum rating must not exceed maximum rating")
    wanted = set(filter(None, args.themes.split(",")))
    if not wanted:
        parser.error("At least one theme is required")

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    scanned = invalid = 0
    stop_reason = "source_exhausted"
    request = urllib.request.Request(SOURCE, headers={"User-Agent": "TacticsLab/0.1 (CC0 puzzle subset import)"})
    with urllib.request.urlopen(request, timeout=45) as response:
        bounded = BoundedReader(response, args.max_download_mb * 1024 * 1024)
        decompressor = zstandard.ZstdDecompressor()
        with decompressor.stream_reader(bounded, read_size=65536) as reader:
            text_stream = io.TextIOWrapper(reader, encoding="utf-8", newline="")
            csv_reader = csv.DictReader(text_stream)
            fields = csv_reader.fieldnames
            if fields is None or not REQUIRED.issubset(fields):
                raise RuntimeError("Source CSV is missing required Lichess columns")
            try:
                for row in csv_reader:
                    scanned += 1
                    try:
                        eligible = (
                            args.min_rating <= int(row["Rating"]) <= args.max_rating
                            and int(row.get("Popularity", "0")) >= 0
                            and int(row.get("NbPlays", "0")) >= args.min_plays
                            and bool(wanted.intersection(row["Themes"].split()))
                            and row["PuzzleId"] not in seen
                        )
                    except (ValueError, KeyError):
                        eligible = False
                    if eligible:
                        if valid_line(row):
                            rows.append(row)
                            seen.add(row["PuzzleId"])
                        else:
                            invalid += 1
                    if len(rows) >= args.limit:
                        stop_reason = "target_reached"
                        break
                    if scanned >= args.max_scan:
                        stop_reason = "scan_limit"
                        break
            except DownloadLimitReached:
                stop_reason = "download_limit"

    if not rows:
        raise RuntimeError("No matching valid puzzles; existing output was not changed")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(args.output)
    counts = collections.Counter(theme for row in rows for theme in row["Themes"].split())
    metadata = {
        "source_url": SOURCE,
        "source_information": "https://database.lichess.org/#puzzles",
        "license": "CC0-1.0",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "puzzles": len(rows),
        "source_rows_scanned": scanned,
        "compressed_bytes_read": bounded.bytes_read,
        "invalid_candidates_skipped": invalid,
        "stop_reason": stop_reason,
        "selection": {
            "requested_count": args.limit,
            "min_rating": args.min_rating,
            "max_rating": args.max_rating,
            "min_plays": args.min_plays,
            "minimum_popularity": 0,
            "any_theme": sorted(wanted),
        },
        "actual_rating_range": [min(int(r["Rating"]) for r in rows), max(int(r["Rating"]) for r in rows)],
        "theme_counts": dict(sorted(counts.items())),
        "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
    }
    args.output.with_suffix(".metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0 if stop_reason == "target_reached" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, zstandard.ZstdError) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        raise SystemExit(1)

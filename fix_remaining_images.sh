#!/usr/bin/env bash
set -euo pipefail

# Round helper for remaining image coverage.
# Artifacts: open_tasks.md + report.md (report round).

mode="${1:---dry-run}"

case "$mode" in
  --dry-run)
    python3 -m tools.image_coverage.run_round --dry-run
    ;;
  --run)
    python3 -m tools.image_coverage.run_round
    ;;
  --help|-h)
    cat <<'USAGE'
Usage:
  ./fix_remaining_images.sh --dry-run
  ./fix_remaining_images.sh --run

Equivalent commands:
  python3 -m tools.image_coverage.run_round --dry-run
  python3 -m tools.image_coverage.run_round
USAGE
    ;;
  *)
    echo "Unknown mode: $mode" >&2
    echo "Use --dry-run, --run, or --help." >&2
    exit 2
    ;;
esac

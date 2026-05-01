#!/usr/bin/env bash
# Run the chill-out CLI against the current directory.
#
# This is the simplest possible workflow: install chill-out, cd into a project,
# and let it tell you which dependencies are still inside their cooldown window.

set -euo pipefail

# Show the resolved cooldown thresholds for the project.
chill-out show-config

# Run the check; exit code 2 means at least one violation was found.
chill-out check --quiet || echo "(violations found — exit $?)"

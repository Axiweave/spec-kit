#!/usr/bin/env bash

# Resolve the active feature and its tasks.md for the github extension.
#
# The github extension owns this script so that `speckit.github.taskstoissues`
# keeps working when the core `taskstoissues` command (and its
# `check-prerequisites` helper invocation) is deprecated and removed. It uses
# core common.sh to resolve the active feature, requires plan.md and
# tasks.md exactly as core's `--require-tasks --include-tasks` invocation does,
# and reports the optional design docs that sit next to them. Core does not
# require spec.md for this command, so neither does this script.
#
# Usage: ./resolve-tasks.sh [--json]
#
# OPTIONS:
#   --json      Output in JSON format
#   --help, -h  Show help message
#
# OUTPUTS:
#   JSON mode: {"FEATURE_DIR":"...","TASKS":"...","AVAILABLE_DOCS":["..."]}
#   Text mode: FEATURE_DIR:... \n TASKS:... \n AVAILABLE_DOCS: \n ✓/✗ file.md

set -e

JSON_MODE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --json)
            JSON_MODE=true
            ;;
        --help|-h)
            cat << 'HELP'
Usage: resolve-tasks.sh [OPTIONS]

Resolve the active feature and its tasks.md for the github extension.

OPTIONS:
  --json      Output in JSON format
  --help, -h  Show this help message

EXAMPLES:
  ./resolve-tasks.sh --json
HELP
            exit 0
            ;;
        *)
            echo "ERROR: Unknown option '$1'. Use --help for usage information." >&2
            exit 1
            ;;
    esac
    shift
done

# Core resolution handles external workspaces, feature_selection and
# SPECIFY_FEATURE_NO_PERSIST exactly as core check-prerequisites does.
# Source: <repo>/extensions/github/scripts/bash -> <repo>/scripts/bash.
# Installed: .specify/extensions/github/scripts/bash -> .specify/scripts/bash.
SCRIPT_DIR="$(CDPATH="" cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_COMMON="$SCRIPT_DIR/../../../../scripts/bash/common.sh"
if [[ ! -f "$CORE_COMMON" ]]; then
    echo "ERROR: Spec Kit core scripts not found: $CORE_COMMON" >&2
    exit 1
fi
source "$CORE_COMMON"
_paths_output=$(get_feature_paths) || exit 1
eval "$_paths_output"
unset _paths_output

if [[ ! -d "$FEATURE_DIR" ]]; then
    echo "ERROR: Feature directory not found: $FEATURE_DIR" >&2
    echo "Run the Spec Kit specify command first to create the feature structure." >&2
    exit 1
fi

IMPL_PLAN="$FEATURE_DIR/plan.md"
if [[ ! -f "$IMPL_PLAN" ]]; then
    echo "ERROR: plan.md not found in $FEATURE_DIR" >&2
    echo "Run the Spec Kit plan command first to create the implementation plan." >&2
    exit 1
fi

TASKS="$FEATURE_DIR/tasks.md"
if [[ ! -f "$TASKS" ]]; then
    echo "ERROR: tasks.md not found in $FEATURE_DIR" >&2
    echo "Run the Spec Kit tasks command first to create the task list." >&2
    exit 1
fi

RESEARCH="$FEATURE_DIR/research.md"
DATA_MODEL="$FEATURE_DIR/data-model.md"
QUICKSTART="$FEATURE_DIR/quickstart.md"
CONTRACTS_DIR="$FEATURE_DIR/contracts"

docs=()
[[ -f "$RESEARCH" ]] && docs+=("research.md")
[[ -f "$DATA_MODEL" ]] && docs+=("data-model.md")
if [[ -d "$CONTRACTS_DIR" ]] && [[ -n "$(ls -A "$CONTRACTS_DIR" 2>/dev/null)" ]]; then
    docs+=("contracts/")
fi
[[ -f "$QUICKSTART" ]] && docs+=("quickstart.md")
docs+=("tasks.md")

if $JSON_MODE; then
    json_docs=$(for d in "${docs[@]}"; do printf '"%s",' "$(json_escape "$d")"; done)
    json_docs="[${json_docs%,}]"
    printf '{"FEATURE_DIR":"%s","TASKS":"%s","AVAILABLE_DOCS":%s}\n' \
        "$(json_escape "$FEATURE_DIR")" "$(json_escape "$TASKS")" "$json_docs"
else
    echo "FEATURE_DIR:$FEATURE_DIR"
    echo "TASKS:$TASKS"
    echo "AVAILABLE_DOCS:"
    for d in "${docs[@]}"; do
        echo "  ✓ $d"
    done
fi

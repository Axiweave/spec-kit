#!/usr/bin/env pwsh

# Resolve the active feature and its tasks.md for the github extension.
#
# The github extension owns this script so that speckit.github.taskstoissues
# keeps working when the core taskstoissues command (and its
# check-prerequisites helper invocation) is deprecated and removed. It uses
# core common.ps1 to resolve the active feature, requires plan.md and
# tasks.md exactly as core's -RequireTasks -IncludeTasks invocation does, and
# reports the optional design docs that sit next to them. Core does not require
# spec.md for this command, so neither does this script.
#
# Usage: ./resolve-tasks.ps1 [-Json]
#
# OPTIONS:
#   -Json       Output in JSON format
#   -Help       Show help message
#
# OUTPUTS:
#   JSON mode: {"FEATURE_DIR":"...","TASKS":"...","AVAILABLE_DOCS":["..."]}
#   Text mode: FEATURE_DIR:... / TASKS:... / AVAILABLE_DOCS: list

[CmdletBinding()]
param(
    [switch]$Json,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

if ($Help) {
    Write-Output @"
Usage: resolve-tasks.ps1 [OPTIONS]

Resolve the active feature and its tasks.md for the github extension.

OPTIONS:
  -Json       Output in JSON format
  -Help       Show this help message

EXAMPLES:
  ./resolve-tasks.ps1 -Json
"@
    exit 0
}

# Core resolution handles external workspaces, feature_selection and
# SPECIFY_FEATURE_NO_PERSIST exactly as core check-prerequisites does.
# Source: <repo>/extensions/github/scripts/powershell -> <repo>/scripts/powershell.
# Installed: .specify/extensions/github/scripts/powershell -> .specify/scripts/powershell.
$coreCommon = Join-Path $PSScriptRoot '../../../../scripts/powershell/common.ps1'
if (-not (Test-Path -LiteralPath $coreCommon -PathType Leaf)) {
    [Console]::Error.WriteLine("ERROR: Spec Kit core scripts not found: $coreCommon")
    exit 1
}
. $coreCommon
$featureDir = (Get-FeaturePathsEnv).FEATURE_DIR

if (-not (Test-Path -LiteralPath $featureDir -PathType Container)) {
    [Console]::Error.WriteLine("ERROR: Feature directory not found: $featureDir")
    [Console]::Error.WriteLine("Run the Spec Kit specify command first to create the feature structure.")
    exit 1
}

$implPlan = Join-Path $featureDir 'plan.md'
if (-not (Test-Path -LiteralPath $implPlan -PathType Leaf)) {
    [Console]::Error.WriteLine("ERROR: plan.md not found in $featureDir")
    [Console]::Error.WriteLine("Run the Spec Kit plan command first to create the implementation plan.")
    exit 1
}

$tasks = Join-Path $featureDir 'tasks.md'
if (-not (Test-Path -LiteralPath $tasks -PathType Leaf)) {
    [Console]::Error.WriteLine("ERROR: tasks.md not found in $featureDir")
    [Console]::Error.WriteLine("Run the Spec Kit tasks command first to create the task list.")
    exit 1
}

$docs = @()
if (Test-Path -LiteralPath (Join-Path $featureDir 'research.md') -PathType Leaf) { $docs += 'research.md' }
if (Test-Path -LiteralPath (Join-Path $featureDir 'data-model.md') -PathType Leaf) { $docs += 'data-model.md' }
$contractsDir = Join-Path $featureDir 'contracts'
if ((Test-Path -LiteralPath $contractsDir -PathType Container) -and
    (Get-ChildItem -LiteralPath $contractsDir -Force -ErrorAction SilentlyContinue | Select-Object -First 1)) {
    $docs += 'contracts/'
}
if (Test-Path -LiteralPath (Join-Path $featureDir 'quickstart.md') -PathType Leaf) { $docs += 'quickstart.md' }
$docs += 'tasks.md'

if ($Json) {
    # -Compress keeps the payload on a single line, matching the bash twin.
    $payload = [ordered]@{
        FEATURE_DIR    = $featureDir
        TASKS          = $tasks
        AVAILABLE_DOCS = @($docs)
    }
    Write-Output ($payload | ConvertTo-Json -Compress)
} else {
    Write-Output "FEATURE_DIR:$featureDir"
    Write-Output "TASKS:$tasks"
    Write-Output "AVAILABLE_DOCS:"
    foreach ($d in $docs) {
        Write-Output "  [OK] $d"
    }
}

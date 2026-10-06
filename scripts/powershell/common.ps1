#!/usr/bin/env pwsh
# Common PowerShell functions analogous to common.sh


# Find repository root by searching upward for .specify directory
# This is the primary marker for spec-kit projects
function Find-SpecifyRoot {
    param([string]$StartDir = (Get-Location).Path)

    # Normalize to absolute path to prevent issues with relative paths
    # Use -LiteralPath to handle paths with wildcard characters ([, ], *, ?)
    $resolved = Resolve-Path -LiteralPath $StartDir -ErrorAction SilentlyContinue
    $current = if ($resolved) { $resolved.Path } else { $null }
    if (-not $current) { return $null }

    while ($true) {
        if (Test-Path -LiteralPath (Join-Path $current ".specify") -PathType Container) {
            return $current
        }
        $parent = Split-Path $current -Parent
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $current) {
            return $null
        }
        $current = $parent
    }
}

# Resolve an explicit SPECIFY_INIT_DIR project override (the directory that
# *contains* .specify/), for non-interactive / CI use -- e.g. running a Spec Kit
# command against a member project from a monorepo root without cd.
#
# Precondition: $env:SPECIFY_INIT_DIR is set. Returns the validated project root,
# or writes an error and exits 1 unless -ReturnNullOnError is set. Strict by
# design: the path must exist and
# contain .specify/, with no silent fallback. (An empty string is falsy, so the
# caller's `if ($env:SPECIFY_INIT_DIR)` guard treats empty as unset.)
#
# This is the single resolver: bundled extensions inherit it by sourcing core
# (e.g. the git extension's create-new-feature-branch) rather than duplicating it.
function Resolve-SpecifyInitDir {
    param([switch]$ReturnNullOnError)

    $initDir = $env:SPECIFY_INIT_DIR
    # Normalize: relative paths resolve against the current directory.
    if (-not [System.IO.Path]::IsPathRooted($initDir)) {
        $initDir = Join-Path (Get-Location).Path $initDir
    }
    $resolved = Resolve-Path -LiteralPath $initDir -ErrorAction SilentlyContinue
    # Resolve-Path also succeeds for files, so check the resolved path is a
    # directory; otherwise a file value would slip through to the less accurate
    # "not a Spec Kit project" error below.
    if (-not $resolved -or -not (Test-Path -LiteralPath $resolved.Path -PathType Container)) {
        [Console]::Error.WriteLine("ERROR: SPECIFY_INIT_DIR does not point to an existing directory: $($env:SPECIFY_INIT_DIR)")
        if ($ReturnNullOnError) { return $null }
        exit 1
    }
    # Resolve-Path echoes back any trailing separator from the input; trim it so
    # the returned root matches the bash resolver, whose `cd && pwd` never yields
    # one. TrimEnd (not [Path]::TrimEndingDirectorySeparator, which is .NET Core
    # only) keeps this working on Windows PowerShell 5.1 / .NET Framework, as
    # Get-FeaturePathsEnv already does below. Unlike a bare TrimEnd, the
    # GetPathRoot check preserves a path that *is* its own root ('C:\' must not
    # become 'C:', which every later API re-resolves against the current
    # directory instead of the drive root). No-op on a path with no trailing
    # separator.
    $initRoot = $resolved.Path.TrimEnd('/', '\')
    if ($initRoot.Length -lt [System.IO.Path]::GetPathRoot($resolved.Path).Length) {
        $initRoot = $resolved.Path
    }
    if (-not (Test-Path -LiteralPath (Join-Path $initRoot '.specify') -PathType Container)) {
        [Console]::Error.WriteLine("ERROR: SPECIFY_INIT_DIR is not a Spec Kit project (no .specify/ directory): $initRoot")
        if ($ReturnNullOnError) { return $null }
        exit 1
    }
    return $initRoot
}

# Get repository root, prioritizing .specify directory
# This prevents using a parent repository when spec-kit is initialized in a subdirectory
function Get-RepoRoot {
    param([switch]$ReturnNullOnError)

    if ($env:SPECIFY_INIT_DIR) {
        $repoRoot = Resolve-SpecifyInitDir -ReturnNullOnError:$ReturnNullOnError
        if (-not $repoRoot) { return $null }
    } else {
        $repoRoot = Find-SpecifyRoot
        if (-not $repoRoot) {
            # Preserve the script-location fallback for local projects only.
            $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "../../..")).Path
        }
    }
    if ((Test-Path -LiteralPath (Join-Path $repoRoot '.specify/workspace.json')) -and
        -not (Test-Path -LiteralPath (Join-Path $repoRoot '.specify/project.json'))) {
        [Console]::Error.WriteLine("ERROR: Use the code repository as the working directory, or set SPECIFY_INIT_DIR to its path.")
        if ($ReturnNullOnError) { return $null }
        exit 1
    }
    return $repoRoot
}

# Resolve existing links, including links in parent directories, without requiring the leaf to exist.
function Get-CanonicalStoragePath {
    param([string]$Path, [int]$Depth = 0)
    if ($Depth -gt 40) { throw "Too many symbolic links in path: $Path" }
    $full = [System.IO.Path]::GetFullPath($Path)
    $parent = Split-Path $full -Parent
    if (-not $parent -or $parent -eq $full) { return $full }
    $resolvedParent = Get-CanonicalStoragePath -Path $parent -Depth $Depth
    $full = Join-Path $resolvedParent (Split-Path $full -Leaf)
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        $target = @($item.Target)[0]
        if (-not $target) { throw "Cannot resolve symbolic link: $full" }
        if (-not [System.IO.Path]::IsPathRooted($target)) { $target = Join-Path $resolvedParent $target }
        return Get-CanonicalStoragePath -Path $target -Depth ($Depth + 1)
    }
    return $full
}

function Resolve-ContainedStoragePath {
    param([string]$Root, [string]$Path)
    if ($Path -match '(^|[\\/])\.\.([\\/]|$)') { throw "Path traversal is not allowed: $Path" }
    $rootPath = [System.IO.Path]::GetFullPath($Root)
    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $rootPath $Path }
    $candidate = [System.IO.Path]::GetFullPath($candidate)
    $comparison = if ($env:OS -eq 'Windows_NT') { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
    $prefix = $rootPath.TrimEnd('/', '\') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $candidate.Equals($rootPath, $comparison) -and -not $candidate.StartsWith($prefix, $comparison)) {
        throw "Path is outside the selected workspace ${Root}: $Path"
    }
    $canonicalRoot = Get-CanonicalStoragePath $rootPath
    $canonical = Get-CanonicalStoragePath $candidate
    $prefix = $canonicalRoot.TrimEnd('/', '\') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $canonical.Equals($canonicalRoot, $comparison) -and -not $canonical.StartsWith($prefix, $comparison)) {
        throw "Symbolic link escapes the selected workspace ${Root}: $Path"
    }
    return $candidate
}

function Read-StorageRecord {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item -or $item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "Storage record must be a regular file: $Path. Relink the workspace with 'specify project link'."
    }
    try {
        $record = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    } catch {
        throw "Cannot read storage record ${Path}: $($_.Exception.Message)"
    }
    if ($record -isnot [PSCustomObject] -or
        ($record.schema_version -isnot [int] -and $record.schema_version -isnot [long]) -or
        $record.schema_version -ne 1) {
        throw "Unsupported storage record schema: $Path"
    }
    return $record
}

function Test-AbsoluteStoragePath {
    param([string]$Path)
    if (-not [System.IO.Path]::IsPathRooted($Path)) { return $false }
    if ($env:OS -eq 'Windows_NT') {
        return $Path -match '^(?:[a-zA-Z]:[\\/]|[\\/]{2}[^\\/]+[\\/][^\\/]+(?:[\\/]|$))'
    }
    return $true
}

function Get-StorageContext {
    param([string]$RepoRoot = (Get-RepoRoot))
    $locatorPath = Join-Path $RepoRoot '.specify/project.json'
    $locatorEntry = Get-Item -LiteralPath $locatorPath -Force -ErrorAction SilentlyContinue
    if (-not $locatorEntry) {
        return [PSCustomObject]@{ Root = $RepoRoot; External = $false; Record = $null; RecordPath = $null }
    }
    $locatorPath = Resolve-ContainedStoragePath $RepoRoot $locatorPath
    $locator = Read-StorageRecord $locatorPath
    if ($locator.storage -cne 'external' -or $locator.project_id -isnot [string] -or
        $locator.project_id -cnotmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') {
        throw "Invalid external project locator: $locatorPath"
    }
    $projectId = ([guid]$locator.project_id).ToString()
    if (-not (Get-Item -LiteralPath (Join-Path $RepoRoot '.specify/checkout.json') -Force -ErrorAction SilentlyContinue)) {
        throw "Missing workspace mapping for $RepoRoot. Use specify project link PATH."
    }
    $recordPath = Resolve-ContainedStoragePath $RepoRoot '.specify/checkout.json'
    $record = Read-StorageRecord $recordPath
    if ($record.workspace -isnot [string] -or -not (Test-AbsoluteStoragePath $record.workspace)) {
        throw "Workspace path must be absolute in storage record: $recordPath"
    }
    $workspace = [System.IO.Path]::GetFullPath($record.workspace)
    [Console]::Error.WriteLine("[specify] Workspace: $workspace")
    if (-not (Test-Path -LiteralPath $workspace -PathType Container)) {
        throw "Selected workspace is unavailable: $workspace. Relink it with 'specify project link'."
    }
    $identityPath = Resolve-ContainedStoragePath $workspace '.specify/workspace.json'
    $identity = Read-StorageRecord $identityPath
    if ($identity.project_id -isnot [string] -or $identity.project_id -ine $projectId) {
        throw "Workspace identity does not match project ${projectId}: $workspace"
    }
    if (-not $record.PSObject.Properties['active_feature'] -or
        ($null -ne $record.active_feature -and
            ($record.active_feature -isnot [string] -or -not $record.active_feature -or
                [System.IO.Path]::IsPathRooted($record.active_feature)))) {
        throw "Active feature must be a relative path or null: $recordPath"
    }
    if ($null -ne $record.active_feature) {
        $null = Resolve-ContainedStoragePath $workspace $record.active_feature
    }
    return [PSCustomObject]@{ Root = $workspace; External = $true; Record = $record; RecordPath = $recordPath }
}

function Resolve-StoragePath {
    param($Storage, [string]$Path)
    if ($Storage.External) { return Resolve-ContainedStoragePath $Storage.Root $Path }
    if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
    return Join-Path $Storage.Root $Path
}

# Read the project's saved-feature policy from the workspace init options.
# Returns 'context' (the default) or 'automatic'. A context project never reads
# or writes the saved feature, so each command names its feature with
# SPECIFY_FEATURE_DIRECTORY. An invalid choice throws before any write.
function Get-FeatureSelectionMode {
    param([Parameter(Mandatory = $true)]$Storage)
    $optionsPath = Resolve-StoragePath $Storage '.specify/init-options.json'
    $item = Get-Item -LiteralPath $optionsPath -Force -ErrorAction SilentlyContinue
    if (-not $item) { return 'context' }
    if ($item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "Cannot read project choices at ${optionsPath}: Project choices must be a regular file."
    }
    $text = [System.IO.File]::ReadAllText($optionsPath, [System.Text.Encoding]::UTF8)
    try {
        $options = $text | ConvertFrom-Json
    } catch {
        throw "Cannot read project choices at ${optionsPath}: $($_.Exception.Message)"
    }
    if (-not $text.TrimStart().StartsWith('{') -or $options -isnot [PSCustomObject]) {
        throw "Cannot read project choices at ${optionsPath}: Project choices must be a JSON object."
    }
    $mode = 'context'
    if ($options.PSObject.Properties.Name -ccontains 'feature_selection') {
        $mode = $options.feature_selection
    }
    if ($mode -isnot [string] -or $mode -cnotin @('context', 'automatic')) {
        throw "feature_selection must be context or automatic: $optionsPath"
    }
    return $mode
}

# Only automatic projects save the active feature, and no-persist always wins.
# SPECIFY_FEATURE_NO_PERSIST is the environment-level equivalent of -NoPersist,
# letting an orchestrator (multi-agent runner, CI matrix) guarantee that no
# script invocation in the process tree writes the saved feature, even scripts
# that don't pass -NoPersist themselves (#4128).
function Test-PersistFeatureSelection {
    param([Parameter(Mandatory = $true)][string]$Mode, [switch]$NoPersist)
    $disabled = [bool]$NoPersist -or $env:SPECIFY_FEATURE_NO_PERSIST -eq '1' -or $env:SPECIFY_FEATURE_NO_PERSIST -eq 'true'
    return ($Mode -ceq 'automatic') -and -not $disabled
}

function Get-CurrentBranch {
    # Return feature name from explicit state only.
    # Feature state is set by SPECIFY_FEATURE (from create-new-feature or
    # the git extension) or, in automatic projects, via .specify/feature.json.
    if ($env:SPECIFY_FEATURE) {
        return $env:SPECIFY_FEATURE
    }

    # No explicit feature set - return empty to signal "unknown".
    return ""
}



# Persist the active feature in the machine record or the local feature.json file.
# Skip writes when the saved selection is unchanged.
function Save-FeatureJson {
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$FeatureDirectory
    )

    $storage = Get-StorageContext -RepoRoot $RepoRoot
    if ($storage.External) {
        $absolute = Resolve-StoragePath $storage $FeatureDirectory
        $relative = $absolute.Substring($storage.Root.TrimEnd('/', '\').Length).TrimStart('/', '\').Replace('\', '/')
        if (-not $relative) { throw "Select a feature directory below the workspace: $absolute" }
        if ($storage.Record.active_feature -ceq $relative) { return }
        $storage.Record.active_feature = $relative
        $temporary = $storage.RecordPath + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
        try {
            $json = $storage.Record | ConvertTo-Json -Depth 100 -Compress
            [System.IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
            [System.IO.File]::Replace($temporary, $storage.RecordPath, [NullString]::Value)
        } finally {
            if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
        }
        return
    }

    # Strip repo root prefix if the value is absolute and under repo root.
    # Use case-insensitive comparison on Windows only (case-sensitive filesystems elsewhere).
    $prefix = $RepoRoot + [System.IO.Path]::DirectorySeparatorChar
    if ($null -ne $IsWindows) { $onWin = $IsWindows } else { $onWin = $true }
    if ($onWin) {
        $cmp = [System.StringComparison]::OrdinalIgnoreCase
    } else {
        $cmp = [System.StringComparison]::Ordinal
    }
    if ($FeatureDirectory.StartsWith($prefix, $cmp)) {
        $FeatureDirectory = $FeatureDirectory.Substring($prefix.Length)
    }

    $fjPath = Join-Path (Join-Path $RepoRoot '.specify') 'feature.json'

    # Read current value and skip write when unchanged
    if (Test-Path -LiteralPath $fjPath -PathType Leaf) {
        try {
            $raw = [System.IO.File]::ReadAllText($fjPath, [System.Text.Encoding]::UTF8)
            $cfg = $raw | ConvertFrom-Json
            if ($cfg.feature_directory -eq $FeatureDirectory) {
                return
            }
        } catch {
            # File is corrupt or unreadable - overwrite it
        }
    }

    # Ensure .specify/ directory exists
    $specifyDir = Join-Path $RepoRoot '.specify'
    if (-not (Test-Path -LiteralPath $specifyDir -PathType Container)) {
        New-Item -ItemType Directory -Path $specifyDir -Force | Out-Null
    }

    # Write feature.json
    $json = @{ feature_directory = $FeatureDirectory } | ConvertTo-Json -Compress
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($fjPath, $json, $utf8NoBom)
}

function Get-FeaturePathsEnv {
    # Read-only callers pass -NoPersist to preserve the saved active feature.
    param(
        [switch]$NoPersist,
        [switch]$ReturnNullOnError
    )

    $repoRoot = Get-RepoRoot -ReturnNullOnError:$ReturnNullOnError
    if (-not $repoRoot) { return $null }
    try {
        $storage = Get-StorageContext -RepoRoot $repoRoot
        $selectionMode = Get-FeatureSelectionMode -Storage $storage
    } catch {
        [Console]::Error.WriteLine("ERROR: $($_.Exception.Message)")
        if ($ReturnNullOnError) { return $null }
        throw
    }
    # Automatic projects save an explicit feature. -NoPersist and the
    # SPECIFY_FEATURE_NO_PERSIST flag always prevent that write.
    $persist = Test-PersistFeatureSelection -Mode $selectionMode -NoPersist:$NoPersist
    $currentBranch = Get-CurrentBranch

    # Resolve feature directory.  Priority:
    #   1. SPECIFY_FEATURE_DIRECTORY env var (explicit override)
    #   2. Automatic projects only: machine-local active_feature in external mode, or local feature.json
    #   3. Error - no feature context available
    if (-not $env:SPECIFY_FEATURE_DIRECTORY -and $selectionMode -cne 'automatic') {
        # Context projects never read the saved feature, so every command names its own.
        [Console]::Error.WriteLine("ERROR: Feature directory not found: no feature is selected. Set SPECIFY_FEATURE_DIRECTORY=specs/<feature> for this command. This project uses feature_selection context, so scripts ignore the saved feature.")
        if ($ReturnNullOnError) { return $null }
        exit 1
    }
    $featureJson = Join-Path $repoRoot '.specify/feature.json'
    if ($storage.External) {
        try {
            $selected = if ($env:SPECIFY_FEATURE_DIRECTORY) { $env:SPECIFY_FEATURE_DIRECTORY } else { $storage.Record.active_feature }
            if (-not $selected) { throw "No active feature in workspace: $($storage.Root). Create or select a feature." }
            $featureDir = Resolve-StoragePath $storage $selected
            $preCreation = $env:SPECIFY_FEATURE_DIRECTORY -and
                ($NoPersist -or $env:SPECIFY_FEATURE_NO_PERSIST -eq '1' -or $env:SPECIFY_FEATURE_NO_PERSIST -eq 'true') -and
                -not (Test-Path -LiteralPath $featureDir)
            if (-not $preCreation -and -not (Test-Path -LiteralPath $featureDir -PathType Container)) {
                throw "Selected feature directory does not exist: $featureDir"
            }
            foreach ($artifact in @('spec.md', 'plan.md', 'tasks.md', 'research.md', 'data-model.md', 'quickstart.md', 'contracts')) {
                $null = Resolve-StoragePath $storage (Join-Path $featureDir $artifact)
            }
            if ($env:SPECIFY_FEATURE_DIRECTORY -and $persist) {
                Save-FeatureJson -RepoRoot $repoRoot -FeatureDirectory $featureDir
            }
        } catch {
            [Console]::Error.WriteLine("ERROR: $($_.Exception.Message)")
            if ($ReturnNullOnError) { return $null }
            throw
        }
    } elseif ($env:SPECIFY_FEATURE_DIRECTORY) {
        $featureDir = $env:SPECIFY_FEATURE_DIRECTORY
        # Normalize relative paths to absolute under repo root
        if (-not [System.IO.Path]::IsPathRooted($featureDir)) {
            $featureDir = Join-Path $repoRoot $featureDir
        }
        # Persist to feature.json so future sessions without the env var still
        # work in automatic projects - unless the caller opted out for read-only
        # resolution (#3025).
        if ($persist) {
            Save-FeatureJson -RepoRoot $repoRoot -FeatureDirectory $env:SPECIFY_FEATURE_DIRECTORY
        }
    } elseif (Test-Path $featureJson) {
        $featureJsonRaw = [System.IO.File]::ReadAllText($featureJson, [System.Text.Encoding]::UTF8)
        try {
            $featureConfig = $featureJsonRaw | ConvertFrom-Json
        } catch {
            [Console]::Error.WriteLine("ERROR: Feature directory not found. Set SPECIFY_FEATURE_DIRECTORY or ensure .specify/feature.json contains feature_directory.")
            if ($ReturnNullOnError) { return $null }
            exit 1
        }
        if ($featureConfig.feature_directory) {
            $featureDir = $featureConfig.feature_directory
            # Normalize relative paths to absolute under repo root
            if (-not [System.IO.Path]::IsPathRooted($featureDir)) {
                $featureDir = Join-Path $repoRoot $featureDir
            }
        } else {
            [Console]::Error.WriteLine("ERROR: Feature directory not found. Set SPECIFY_FEATURE_DIRECTORY or ensure .specify/feature.json contains feature_directory.")
            if ($ReturnNullOnError) { return $null }
            exit 1
        }
    } else {
        [Console]::Error.WriteLine("ERROR: Feature directory not found. Set SPECIFY_FEATURE_DIRECTORY or run the specify command to create .specify/feature.json.")
        if ($ReturnNullOnError) { return $null }
        exit 1
    }

    # When no branch context exists (no SPECIFY_FEATURE, feature resolved via
    # SPECIFY_FEATURE_DIRECTORY or feature.json), fall back to the feature
    # directory basename so CURRENT_BRANCH is a usable identifier rather than
    # an empty, misleading value (issue #3026).
    if (-not $currentBranch) {
        # TrimEnd (not [Path]::TrimEndingDirectorySeparator, which is .NET Core
        # only) keeps this working on Windows PowerShell 5.1 / .NET Framework.
        $featureDirTrimmed = $featureDir.TrimEnd('/', '\')
        $currentBranch = Split-Path -Leaf $featureDirTrimmed
    }

    [PSCustomObject]@{
        REPO_ROOT     = $repoRoot
        WORKSPACE_ROOT = $storage.Root
        CURRENT_BRANCH = $currentBranch
        FEATURE_DIR   = $featureDir
        FEATURE_SPEC  = Join-Path $featureDir 'spec.md'
        IMPL_PLAN     = Join-Path $featureDir 'plan.md'
        TASKS         = Join-Path $featureDir 'tasks.md'
        RESEARCH      = Join-Path $featureDir 'research.md'
        DATA_MODEL    = Join-Path $featureDir 'data-model.md'
        QUICKSTART    = Join-Path $featureDir 'quickstart.md'
        CONTRACTS_DIR = Join-Path $featureDir 'contracts'
    }
}

function Test-FileExists {
    param([string]$Path, [string]$Description)
    if (Test-Path -Path $Path -PathType Leaf) {
        Write-Output "  [OK] $Description"
        return $true
    } else {
        Write-Output "  [FAIL] $Description"
        return $false
    }
}

function Test-DirHasFiles {
    param([string]$Path, [string]$Description)
    # A directory counts as non-empty when Get-ChildItem returns any entry
    # (files or subdirectories) -- matching the JSON contracts checks in
    # check-prerequisites.ps1 / setup-tasks.ps1, and treating a directory whose
    # only contents are subdirectories (e.g. contracts/v1/openapi.yaml) as
    # non-empty like bash check_dir. Filtering out subdirectories would
    # mis-report such a directory as empty.
    if ((Test-Path -Path $Path -PathType Container) -and (Get-ChildItem -Path $Path -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        Write-Output "  [OK] $Description"
        return $true
    } else {
        Write-Output "  [FAIL] $Description"
        return $false
    }
}

function Get-InvokeSeparator {
    param([string]$RepoRoot = (Get-RepoRoot))
    $storage = Get-StorageContext -RepoRoot $RepoRoot

    if ($null -eq $script:SpecKitInvokeSeparatorCache) {
        $script:SpecKitInvokeSeparatorCache = @{}
    }
    if ($script:SpecKitInvokeSeparatorCache.ContainsKey($RepoRoot)) {
        return $script:SpecKitInvokeSeparatorCache[$RepoRoot]
    }

    $separator = '.'
    $integrationJson = Resolve-StoragePath $storage '.specify/integration.json'
    if (Test-Path -LiteralPath $integrationJson -PathType Leaf) {
        try {
            $state = Get-Content -LiteralPath $integrationJson -Raw | ConvertFrom-Json
            $key = if ($state.default_integration) { [string]$state.default_integration } elseif ($state.integration) { [string]$state.integration } else { '' }
            if ($key -and $state.integration_settings) {
                $settingProperty = $state.integration_settings.PSObject.Properties[$key]
                if ($settingProperty) {
                    $setting = $settingProperty.Value
                    if ($setting -and ($setting.invoke_separator -eq '.' -or $setting.invoke_separator -eq '-')) {
                        $separator = [string]$setting.invoke_separator
                    }
                }
            }
        } catch {
            $separator = '.'
        }
    }

    $script:SpecKitInvokeSeparatorCache[$RepoRoot] = $separator
    return $separator
}

function Format-SpecKitCommand {
    param(
        [Parameter(Mandatory = $true)][string]$CommandName,
        [string]$RepoRoot = (Get-RepoRoot)
    )

    $separator = Get-InvokeSeparator -RepoRoot $RepoRoot
    $name = $CommandName.TrimStart('/')
    if ($name.StartsWith('speckit.')) {
        $name = $name.Substring(8)
    } elseif ($name.StartsWith('speckit-')) {
        $name = $name.Substring(8)
    }
    $name = $name -replace '\.', $separator

    return "/speckit$separator$name"
}

# Find a usable Python 3 executable (python3, python, or py -3).
# Returns the command/arguments as an array, or $null if none found.
function Get-Python3Command {
    # SPECKIT_PYTHON_EXECUTABLE is the canonical override; SPECKIT_PYTHON is
    # kept as a deprecated fallback (still used by update-agent-context.sh).
    $override = if ($env:SPECKIT_PYTHON_EXECUTABLE) { $env:SPECKIT_PYTHON_EXECUTABLE } else { $env:SPECKIT_PYTHON }
    if ($override -and (Get-Command $override -ErrorAction SilentlyContinue)) {
        $ver = & $override --version 2>&1
        if ($ver -match 'Python 3') {
            & $override -c 'import yaml' *> $null
            if ($LASTEXITCODE -eq 0) { return @($override) }
        }
    }
    if (Get-Command python3 -ErrorAction SilentlyContinue) { return @('python3') }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $ver = & python --version 2>&1
        if ($ver -match 'Python 3') { return @('python') }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $ver = & py -3 --version 2>&1
        if ($ver -match 'Python 3') { return @('py', '-3') }
    }
    return $null
}

function Get-NormalizedPriority {
    param($Value)

    if ($Value -is [bool]) { return 10 }
    if ($Value -is [string]) {
        $integerText = $Value.Trim()
        if ($integerText -cnotmatch '^[+-]?[0-9]+(?:_[0-9]+)*$') { return 10 }
        $Value = $integerText.Replace('_', '')
    }
    try {
        $parsedPriority = [System.Numerics.BigInteger]$Value
    } catch {
        return 10
    }
    return $(if ($parsedPriority -ge 1) { $parsedPriority } else { 10 })
}

function Get-SortedExtensionIds {
    param([Parameter(Mandatory=$true)][string]$ExtensionsDir)

    $registeredNames = @()
    $ranked = @()
    $registryFile = Join-Path $ExtensionsDir '.registry'
    # Detect any filesystem entry at the registry path without following symlinks.
    # Test-Path follows links and reports $false for a dangling symlink, so a
    # broken .registry symlink would otherwise bypass this guard and let the
    # directory scan below enable every on-disk extension. Enumerating the parent
    # directory still observes a broken symlink as an entry.
    $registryEntry = Get-ChildItem -LiteralPath $ExtensionsDir -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq '.registry' } |
        Select-Object -First 1
    if ($registryEntry) {
        if (-not (Test-Path -LiteralPath $registryFile -PathType Leaf)) {
            throw "Invalid extension registry ${registryFile}: not a regular file"
        }
        try {
            $data = [System.IO.File]::ReadAllText($registryFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        } catch {
            throw "Invalid extension registry ${registryFile}: $($_.Exception.Message)"
        }
        if ($null -eq $data -or $data -isnot [PSCustomObject]) {
            throw "Invalid extension registry ${registryFile}: root must be a mapping"
        }
        $extensionsProperty = $data.PSObject.Properties['extensions']
        if ($extensionsProperty) {
            if ($extensionsProperty.Value -isnot [PSCustomObject]) {
                throw "Invalid extension registry ${registryFile}: 'extensions' must be a mapping"
            }
            $extensions = $extensionsProperty.Value
        } else {
            $extensions = [PSCustomObject]@{}
        }
        $registeredNames = @($extensions.PSObject.Properties | ForEach-Object { $_.Name })
        foreach ($entry in $extensions.PSObject.Properties) {
            if ($entry.Name -cnotmatch '^[a-z0-9-]+$' -or $entry.Value -isnot [PSCustomObject]) {
                continue
            }
            $enabledProperty = $entry.Value.PSObject.Properties['enabled']
            if ($enabledProperty -and -not [bool]$enabledProperty.Value) { continue }
            $priority = 10
            $priorityProperty = $entry.Value.PSObject.Properties['priority']
            if ($priorityProperty) {
                $priority = Get-NormalizedPriority -Value $priorityProperty.Value
            }
            $ranked += [PSCustomObject]@{ Priority = $priority; Id = $entry.Name }
        }
    }

    foreach ($directory in Get-ChildItem -Path $ExtensionsDir -Directory -ErrorAction SilentlyContinue) {
        if ($directory.Name -cmatch '^[a-z0-9-]+$' -and $directory.Name -cnotin $registeredNames) {
            $ranked += [PSCustomObject]@{ Priority = 10; Id = $directory.Name }
        }
    }
    return $ranked | Sort-Object Priority, Id | ForEach-Object { $_.Id }
}

# Resolve a template name to a file path using the priority stack:
#   1. .specify/templates/overrides/
#   2. .specify/presets/<preset-id>/templates/ (sorted by priority from .registry)
#   3. .specify/extensions/<ext-id>/templates/
#   4. .specify/templates/ (core)
function Resolve-Template {
    param(
        [Parameter(Mandatory=$true)][string]$TemplateName,
        [Parameter(Mandatory=$true)][string]$RepoRoot
    )

    if ($TemplateName -cnotmatch '^[a-z0-9-]+$') { return $null }
    $storage = Get-StorageContext -RepoRoot $RepoRoot

    $base = Resolve-StoragePath $storage '.specify/templates'

    # Priority 1: Project overrides
    $override = Resolve-StoragePath $storage (Join-Path $base "overrides/$TemplateName.md")
    if (Test-Path $override) { return $override }

    # Priority 2: Installed presets (sorted by priority from .registry)
    $presetsDir = Resolve-StoragePath $storage '.specify/presets'
    if (Test-Path $presetsDir) {
        $registryFile = Resolve-StoragePath $storage (Join-Path $presetsDir '.registry')
        $sortedPresets = @()
        $registryParsed = $false
        if (Test-Path $registryFile) {
            try {
                $registryData = [System.IO.File]::ReadAllText($registryFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
                if ($null -eq $registryData -or $registryData -isnot [PSCustomObject]) {
                    throw 'Registry root must be an object'
                }
                $presetsProperty = $registryData.PSObject.Properties['presets']
                if ($presetsProperty) {
                    $presets = $presetsProperty.Value
                    if ($null -eq $presets -or $presets -isnot [PSCustomObject]) {
                        throw 'Registry presets must be an object'
                    }
                    $presetEntries = @($presets.PSObject.Properties)
                    $priorityFor = {
                        param($Entry)
                        if ($Entry.Value -is [PSCustomObject]) {
                            $priorityProperty = $Entry.Value.PSObject.Properties['priority']
                            if ($priorityProperty) {
                                return Get-NormalizedPriority -Value $priorityProperty.Value
                            }
                        }
                        return 10
                    }
                    $sortedPresets = $presetEntries |
                        Where-Object { $_.Value -is [PSCustomObject] } |
                        Where-Object {
                            $enabled = $_.Value.PSObject.Properties['enabled']
                            -not $enabled -or [bool]$enabled.Value
                        } |
                        Where-Object { $_.Name -cmatch '^[a-z0-9-]+$' } |
                        Sort-Object @{ Expression = { & $priorityFor $_ } }, @{ Expression = { $_.Name } } |
                        ForEach-Object { $_.Name }
                }
                $registryParsed = $true
            } catch {
                $registryParsed = $false
            }
        }

        if ($registryParsed) {
            foreach ($presetId in $sortedPresets) {
                $candidate = Resolve-StoragePath $storage (Join-Path $presetsDir "$presetId/templates/$TemplateName.md")
                if (Test-Path $candidate) { return $candidate }
                $candidate = Resolve-StoragePath $storage (Join-Path $presetsDir "$presetId/$TemplateName.md")
                if (Test-Path $candidate) { return $candidate }
            }
        } else {
            # Fallback: alphabetical directory order
            foreach ($preset in Get-ChildItem -Path $presetsDir -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike '.*' } | Sort-Object Name) {
                $candidate = Resolve-StoragePath $storage (Join-Path $preset.FullName "templates/$TemplateName.md")
                if (Test-Path $candidate) { return $candidate }
                $candidate = Resolve-StoragePath $storage (Join-Path $preset.FullName "$TemplateName.md")
                if (Test-Path $candidate) { return $candidate }
            }
        }
    }

    # Priority 3: Extension-provided templates
    $extDir = Resolve-StoragePath $storage '.specify/extensions'
    if (Test-Path $extDir) {
        $null = Resolve-StoragePath $storage (Join-Path $extDir '.registry')
        foreach ($extensionId in Get-SortedExtensionIds -ExtensionsDir $extDir) {
            $candidate = Resolve-StoragePath $storage (Join-Path $extDir "$extensionId/templates/$TemplateName.md")
            if (-not (Test-Path $candidate)) {
                $candidate = Resolve-StoragePath $storage (Join-Path $extDir "$extensionId/$TemplateName.md")
            }
            if (Test-Path $candidate) { return $candidate }
        }
    }

    # Priority 4: Core templates
    $core = Resolve-StoragePath $storage (Join-Path $base "$TemplateName.md")
    if (Test-Path $core) { return $core }

    return $null
}

# Resolve a template name to composed content using composition strategies.
# Reads strategy metadata from preset manifests and composes content
# from multiple layers using prepend, append, or wrap strategies.
function Resolve-TemplateContent {
    param(
        [Parameter(Mandatory=$true)][string]$TemplateName,
        [Parameter(Mandatory=$true)][string]$RepoRoot
    )

    if ($TemplateName -cnotmatch '^[a-z0-9-]+$') {
        return $null
    }
    $storage = Get-StorageContext -RepoRoot $RepoRoot

    $base = Resolve-StoragePath $storage '.specify/templates'

    # Collect all layers (highest priority first)
    $layerPaths = @()
    $layerStrategies = @()

    # Priority 1: Project overrides (always "replace")
    $override = Resolve-StoragePath $storage (Join-Path $base "overrides/$TemplateName.md")
    if (Test-Path $override) {
        return [System.IO.File]::ReadAllText(
            $override,
            [System.Text.Encoding]::UTF8
        )
    }

    $effectiveBaseFound = $false

    # Priority 2: Installed presets (sorted by priority from .registry)
    $presetsDir = Resolve-StoragePath $storage '.specify/presets'
    if (Test-Path $presetsDir) {
        $registryFile = Resolve-StoragePath $storage (Join-Path $presetsDir '.registry')
        $sortedPresets = @()
        $registryParsed = $false
        if (Test-Path $registryFile) {
            try {
                $registryData = [System.IO.File]::ReadAllText($registryFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
                if ($null -eq $registryData -or $registryData -isnot [PSCustomObject]) {
                    throw 'Registry root must be an object'
                }
                $presetsProperty = $registryData.PSObject.Properties['presets']
                if ($presetsProperty) {
                    $presets = $presetsProperty.Value
                    if ($null -eq $presets -or $presets -isnot [PSCustomObject]) {
                        throw 'Registry presets must be an object'
                    }
                    $presetEntries = @($presets.PSObject.Properties)
                    $priorityFor = {
                        param($Entry)
                        if ($Entry.Value -is [PSCustomObject]) {
                            $priorityProperty = $Entry.Value.PSObject.Properties['priority']
                            if ($priorityProperty) {
                                return Get-NormalizedPriority -Value $priorityProperty.Value
                            }
                        }
                        return 10
                    }
                    $sortedPresets = $presetEntries |
                        Where-Object { $_.Value -is [PSCustomObject] } |
                        Where-Object {
                            $enabled = $_.Value.PSObject.Properties['enabled']
                            -not $enabled -or [bool]$enabled.Value
                        } |
                        Where-Object { $_.Name -cmatch '^[a-z0-9-]+$' } |
                        Sort-Object @{ Expression = { & $priorityFor $_ } }, @{ Expression = { $_.Name } } |
                        ForEach-Object { $_.Name }
                }
                $registryParsed = $true
            } catch {
                $registryParsed = $false
            }
        }

        if (-not $registryParsed) {
            $sortedPresets = Get-ChildItem -Path $presetsDir -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -cmatch '^[a-z0-9-]+$' } |
                Sort-Object Name |
                ForEach-Object { $_.Name }
        }

        $pyCmd = @(Get-Python3Command)
        foreach ($presetId in $sortedPresets) {
                # Read strategy and file path from preset manifest
                $strategy = 'replace'
                $manifestFilePath = ''
                $manifestDeclared = $false
                $manifest = Resolve-StoragePath $storage (Join-Path $presetsDir "$presetId/preset.yml")
                if ((Test-Path $manifest) -and -not $pyCmd) {
                    throw "Python 3 and PyYAML are required to resolve preset template composition"
                }
                if (Test-Path $manifest) {
                    try {
                        # Use Python to parse YAML manifest for strategy and file path
                        $pyArgs = if ($pyCmd.Count -gt 1) { $pyCmd[1..($pyCmd.Count-1)] } else { @() }
                        $pyStderrFile = [System.IO.Path]::GetTempFileName()
                        $stratResult = & $pyCmd[0] @pyArgs -c @"
import sys
try:
    import yaml
except ImportError:
    print('yaml_missing', file=sys.stderr)
    sys.exit(2)
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError('manifest root must be a mapping')
    if 'provides' not in data:
        raise ValueError('manifest missing provides section')
    provides = data['provides']
    if not isinstance(provides, dict):
        raise ValueError('manifest provides must be a mapping')
    if 'templates' not in provides:
        raise ValueError('manifest provides missing templates')
    templates = provides['templates']
    if not isinstance(templates, list):
        raise ValueError('manifest templates must be a list')
    if not templates:
        raise ValueError('manifest must provide at least one template')
    valid_types = ('template', 'command', 'script')
    valid_strategies = ('replace', 'prepend', 'append', 'wrap')
    for t in templates:
        if not isinstance(t, dict):
            raise ValueError('manifest template entries must be mappings')
        if 'type' not in t or 'name' not in t or 'file' not in t:
            raise ValueError('manifest template entry missing type, name, or file')
        for field in ('type', 'name', 'file'):
            if not isinstance(t[field], str):
                raise ValueError('manifest template ' + field + ' must be a string')
        if t['type'] not in valid_types:
            raise ValueError('invalid manifest template type')
        strategy = t.get('strategy', 'replace')
        if not isinstance(strategy, str):
            raise ValueError('manifest template strategy must be a string')
        strategy = strategy.lower()
        if strategy not in valid_strategies:
            raise ValueError('invalid manifest template strategy')
        if t['type'] == 'script' and strategy not in ('replace', 'wrap'):
            raise ValueError('invalid manifest script strategy')
    for t in templates:
        if t.get('name') == sys.argv[2] and t.get('type', 'template') == 'template':
            file_value = t.get('file', '')
            strategy = t.get('strategy', 'replace')
            print('found\t' + strategy + '\t' + file_value)
            sys.exit(0)
    print('absent\treplace\t')
except Exception as exc:
    print(f'manifest_invalid: {exc}', file=sys.stderr)
    sys.exit(3)
"@ $manifest $TemplateName 2>$pyStderrFile
                        if ($LASTEXITCODE -ne 0) {
                            if ($LASTEXITCODE -eq 2) {
                                throw "PyYAML is required to resolve preset template composition"
                            }
                            throw "Invalid preset manifest $manifest"
        }
                        if ($stratResult) {
                            $parts = $stratResult.Trim() -split "`t", 3
                            $manifestDeclared = $parts[0] -eq 'found'
                            $strategy = $parts[1].ToLowerInvariant()
                            if ($parts.Count -gt 2 -and $parts[2]) { $manifestFilePath = $parts[2] }
                        }
                        Remove-Item $pyStderrFile -Force -ErrorAction SilentlyContinue
                    } catch {
                        if ($pyStderrFile) { Remove-Item $pyStderrFile -Force -ErrorAction SilentlyContinue }
                        throw
                    }
                }
                # Try manifest file path first, then convention path
                $candidate = $null
                if ($manifestFilePath) {
                    # Reject absolute paths and parent traversal
                    if ([System.IO.Path]::IsPathRooted($manifestFilePath) -or $manifestFilePath -match '\.\.[\\/]') {
                        if ($storage.External) { throw "Invalid template path in ${manifest}: $manifestFilePath" }
                        $manifestFilePath = ''
                    }
                }
                if ($manifestFilePath) {
                    $mf = Resolve-StoragePath $storage (Join-Path $presetsDir "$presetId/$manifestFilePath")
                    if (Test-Path $mf) { $candidate = $mf }
                }
                if (-not $candidate -and -not $manifestDeclared) {
                    $cf = Resolve-StoragePath $storage (Join-Path $presetsDir "$presetId/templates/$TemplateName.md")
                    if (Test-Path $cf) { $candidate = $cf }
                    if (-not $candidate) {
                        $cf = Resolve-StoragePath $storage (Join-Path $presetsDir "$presetId/$TemplateName.md")
                        if (Test-Path $cf) { $candidate = $cf }
                    }
                }
                if ($candidate) {
                    $layerPaths += $candidate
                    $layerStrategies += $strategy
                    if ($strategy -eq 'replace') {
                        $effectiveBaseFound = $true
                        break
                    }
                }
            }
    }

    # Priority 3: Extension-provided templates (always "replace")
    $extDir = Resolve-StoragePath $storage '.specify/extensions'
    if (-not $effectiveBaseFound -and (Test-Path $extDir)) {
        $null = Resolve-StoragePath $storage (Join-Path $extDir '.registry')
        foreach ($extensionId in Get-SortedExtensionIds -ExtensionsDir $extDir) {
            $candidate = Resolve-StoragePath $storage (Join-Path $extDir "$extensionId/templates/$TemplateName.md")
            if (-not (Test-Path $candidate)) {
                $candidate = Resolve-StoragePath $storage (Join-Path $extDir "$extensionId/$TemplateName.md")
            }
            if (Test-Path $candidate) {
                $layerPaths += $candidate
                $layerStrategies += 'replace'
                $effectiveBaseFound = $true
                break
            }
        }
    }

    # Priority 4: Core templates (always "replace")
    $core = Resolve-StoragePath $storage (Join-Path $base "$TemplateName.md")
    if (-not $effectiveBaseFound -and (Test-Path $core)) {
        $layerPaths += $core
        $layerStrategies += 'replace'
    }

    if ($layerPaths.Count -eq 0) { return $null }

    # If the top (highest-priority) layer is replace, it wins entirely --
    # lower layers are irrelevant regardless of their strategies.
    if ($layerStrategies[0] -eq 'replace') {
        return [System.IO.File]::ReadAllText($layerPaths[0], [System.Text.Encoding]::UTF8)
    }

    # Check if any layer uses a non-replace strategy
    $hasComposition = $false
    foreach ($s in $layerStrategies) {
        if ($s -ne 'replace') { $hasComposition = $true; break }
    }

    if (-not $hasComposition) {
        return [System.IO.File]::ReadAllText($layerPaths[0], [System.Text.Encoding]::UTF8)
    }

    # Find the effective base: scan from highest priority (index 0) downward
    # to find the nearest replace layer. Only compose layers above that base.
    $baseIdx = -1
    for ($i = 0; $i -lt $layerPaths.Count; $i++) {
        if ($layerStrategies[$i] -eq 'replace') {
            $baseIdx = $i
            break
        }
    }
    if ($baseIdx -lt 0) {
        throw "Template '$TemplateName' has composing layers but no replace base"
    }

    $content = [System.IO.File]::ReadAllText(
        $layerPaths[$baseIdx],
        [System.Text.Encoding]::UTF8
    )

    for ($i = $baseIdx - 1; $i -ge 0; $i--) {
        $path = $layerPaths[$i]
        $strat = $layerStrategies[$i]
        $layerContent = [System.IO.File]::ReadAllText(
            $path,
            [System.Text.Encoding]::UTF8
        )

        switch ($strat) {
            'replace' { $content = $layerContent }
            'prepend' { $content = "$layerContent`n`n$content" }
            'append'  { $content = "$content`n`n$layerContent" }
            'wrap'    {
                if (-not $layerContent.Contains('{CORE_TEMPLATE}')) {
                    throw "Wrap strategy missing {CORE_TEMPLATE} placeholder"
                }
                $content = $layerContent.Replace('{CORE_TEMPLATE}', $content)
            }
            default { throw "Unknown strategy: $strat" }
        }
    }

    return $content
}

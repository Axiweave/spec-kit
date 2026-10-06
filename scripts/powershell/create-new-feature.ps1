#!/usr/bin/env pwsh
# Create a new feature
[CmdletBinding()]
param(
    [switch]$Json,
    [switch]$AllowExistingBranch,
    [switch]$DryRun,
    [string]$ShortName,
    [Parameter()]
    [string]$Number = '',
    [switch]$Timestamp,
    [switch]$Help,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$FeatureDescription
)
$ErrorActionPreference = 'Stop'
$maxBranchLength = 244

# Show help if requested
if ($Help) {
    Write-Host "Usage: ./create-new-feature.ps1 [-Json] [-DryRun] [-AllowExistingBranch] [-ShortName <name>] [-Number N] [-Timestamp] <feature description>"
    Write-Host ""
    Write-Host "Options:"
    Write-Host "  -Json               Output in JSON format"
    Write-Host "  -DryRun             Compute feature name and paths without creating directories or files"
    Write-Host "  -AllowExistingBranch  Reuse an existing feature directory if it already exists"
    Write-Host "  -ShortName <name>   Provide a custom short name (2-4 words) for the feature"
    Write-Host "  -Number N           Prefer a feature number (auto-corrected if its specs prefix exists)"
    Write-Host "  -Timestamp          Use timestamp prefix (YYYYMMDD-HHMMSS) instead of sequential numbering"
    Write-Host "  -Help               Show this help message"
    Write-Host ""
    Write-Host "Examples:"
    Write-Host "  ./create-new-feature.ps1 'Add user authentication system' -ShortName 'user-auth'"
    Write-Host "  ./create-new-feature.ps1 'Implement OAuth2 integration for API'"
    Write-Host "  ./create-new-feature.ps1 -Timestamp -ShortName 'user-auth' 'Add user authentication'"
    exit 0
}

# Check if feature description provided
if (-not $FeatureDescription -or $FeatureDescription.Count -eq 0) {
    Write-Error "Usage: ./create-new-feature.ps1 [-Json] [-DryRun] [-AllowExistingBranch] [-ShortName <name>] [-Number N] [-Timestamp] <feature description>"
    exit 1
}

$featureDesc = ($FeatureDescription -join ' ').Trim()

# Validate description is not empty after trimming (e.g., user passed only whitespace)
if ([string]::IsNullOrWhiteSpace($featureDesc)) {
    Write-Error "Error: Feature description cannot be empty or contain only whitespace"
    exit 1
}

function Get-HighestNumberFromSpecs {
    param([string]$SpecsDir)

    [long]$highest = 0
    if (Test-Path $SpecsDir) {
        Get-ChildItem -Path $SpecsDir -Directory | ForEach-Object {
            # Match sequential prefixes (>=3 digits), but skip timestamp dirs.
            if ($_.Name -match '^(\d{3,})-' -and $_.Name -notmatch '^\d{8}-\d{6}-') {
                [long]$num = 0
                if ([long]::TryParse($matches[1], [ref]$num) -and $num -gt $highest) {
                    $highest = $num
                }
            }
        }
    }
    return $highest
}

# Return whether a spec directory other than the optional own name owns the
# given numeric prefix. This is also the tie rescan after an exclusive claim.
function Test-SpecPrefixInUse {
    param(
        [string]$SpecsDir,
        [string]$FeatureNum,
        [string]$OwnName = ''
    )

    if (-not (Test-Path -LiteralPath $SpecsDir -PathType Container)) {
        return $false
    }

    return $null -ne (Get-ChildItem -LiteralPath $SpecsDir -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "$FeatureNum-*" -and $_.Name -cne $OwnName } |
        Select-Object -First 1)
}

function ConvertTo-AsciiLower {
    param([string]$Name)

    return [regex]::Replace($Name, '[A-Z]', { param($match) $match.Value.ToLowerInvariant() })
}

function ConvertTo-UnicodeWords {
    param([string]$Name, [string]$Separator)

    $lowerName = ConvertTo-AsciiLower -Name $Name
    return [regex]::Replace($lowerName, '[\uD800-\uDBFF][\uDC00-\uDFFF]|[^\p{L}\p{Nd}]', {
        param($match)
        if ($match.Length -eq 2) {
            $category = [System.Globalization.CharUnicodeInfo]::GetUnicodeCategory($match.Value, 0)
            if ($category.ToString() -match '(Letter|DecimalDigitNumber)$') {
                return $match.Value
            }
        }
        return $Separator
    })
}

function ConvertTo-CleanBranchName {
    param([string]$Name)

    return (ConvertTo-UnicodeWords -Name $Name -Separator '-') -replace '-{2,}', '-' -replace '^-', '' -replace '-$', ''
}

function Get-FittedBranchName {
    param(
        [string]$FeatureNum,
        [string]$BranchSuffix
    )

    $fittedName = "$FeatureNum-$BranchSuffix"
    if ([System.Text.Encoding]::UTF8.GetByteCount($fittedName) -gt $maxBranchLength) {
        $prefixLength = $FeatureNum.Length + 1
        $maxSuffixLength = $maxBranchLength - $prefixLength
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($BranchSuffix)
        $bytesToUse = $maxSuffixLength
        while ($bytesToUse -gt 0 -and ($bytes[$bytesToUse] -band 0xC0) -eq 0x80) {
            $bytesToUse--
        }
        $truncatedSuffix = [System.Text.Encoding]::UTF8.GetString($bytes, 0, $bytesToUse)
        $truncatedSuffix = $truncatedSuffix -replace '-$', ''
        $fittedName = "$FeatureNum-$truncatedSuffix"
    }

    return $fittedName
}
# Load common functions (includes Get-RepoRoot and Resolve-Template)
. "$PSScriptRoot/common.ps1"

# Use common.ps1 functions which prioritize .specify
$repoRoot = Get-RepoRoot
$storage = Get-StorageContext -RepoRoot $repoRoot
# Read the saved-feature policy before any write so an invalid choice creates nothing.
$selectionMode = Get-FeatureSelectionMode -Storage $storage

# Explicit per-feature choices take precedence over saved project choices.
$hasNumber = $PSBoundParameters.ContainsKey('Number') -and $Number -ne ''
if (-not $Timestamp -and -not $hasNumber) {
    $optionsPath = Resolve-StoragePath $storage '.specify/init-options.json'
    if (Test-Path -LiteralPath $optionsPath -PathType Leaf) {
        $text = [System.IO.File]::ReadAllText($optionsPath, [System.Text.Encoding]::UTF8)
        $options = $text | ConvertFrom-Json
        if (-not $text.TrimStart().StartsWith('{') -or $options -isnot [PSCustomObject]) {
            throw "Project choices must be a JSON object: $optionsPath"
        }
        $numbering = 'sequential'
        if ($options.PSObject.Properties.Name -ccontains 'feature_numbering') {
            $numbering = $options.feature_numbering
        }
        if ($numbering -isnot [string] -or $numbering -cnotin @('sequential', 'timestamp')) {
            throw "Feature numbering must be sequential or timestamp: $optionsPath"
        }
        $Timestamp = $numbering -ceq 'timestamp'
    }
}

Set-Location $repoRoot

$specsDir = Resolve-StoragePath $storage 'specs'
if (-not $DryRun) {
    New-Item -ItemType Directory -Path $specsDir -Force | Out-Null
}

# Function to generate branch name with stop word filtering and length filtering
function Get-BranchName {
    param([string]$Description)

    # Common stop words to filter out
    $stopWords = @(
        'i', 'a', 'an', 'the', 'to', 'for', 'of', 'in', 'on', 'at', 'by', 'with', 'from',
        'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
        'do', 'does', 'did', 'will', 'would', 'should', 'could', 'can', 'may', 'might', 'must', 'shall',
        'this', 'that', 'these', 'those', 'my', 'your', 'our', 'their',
        'want', 'need', 'add', 'get', 'set'
    )

    # Lowercase ASCII and extract Unicode words, matching the shell variant.
    $cleanName = ConvertTo-UnicodeWords -Name $Description -Separator ' '
    $words = $cleanName -split '\s+' | Where-Object { $_ }

    # Filter words: remove stop words and words shorter than 3 chars (unless they're uppercase acronyms in original)
    $meaningfulWords = @()
    foreach ($word in $words) {
        # Skip stop words
        if ($stopWords -ccontains $word) { continue }

        # Keep Unicode words even when short; ASCII words still need three
        # characters or an uppercase acronym in the original.
        if ($word.Length -ge 3 -or $word -match '[^\x00-\x7F]') {
            $meaningfulWords += $word
        } elseif ($Description -cmatch "(?<![0-9A-Za-z_])$($word.ToUpper())(?![0-9A-Za-z_])") {
            # Keep short words only if they appear as uppercase in original (likely
            # acronyms). Use -cmatch so the comparison is case-sensitive, matching the
            # bash script's case-sensitive grep; -match would be case-insensitive and
            # would keep every short word. The boundaries are spelled out as ASCII
            # because .NET's \b is Unicode-aware, so an accented letter next to the
            # acronym would suppress the match that bash's LC_ALL=C grep still makes.
            $meaningfulWords += $word
        }
    }

    # If we have meaningful words, use first 3-4 of them
    if ($meaningfulWords.Count -gt 0) {
        $maxWords = if ($meaningfulWords.Count -eq 4) { 4 } else { 3 }
        $result = ($meaningfulWords | Select-Object -First $maxWords) -join '-'
        return $result
    } else {
        # Fallback to original logic if no meaningful words found
        $result = ConvertTo-CleanBranchName -Name $Description
        # @() keeps this an array when the description contains only separators.
        $fallbackWords = @(($result -split '-') | Where-Object { $_ } | Select-Object -First 3)
        return [string]::Join('-', $fallbackWords)
    }
}

# Generate branch name
if ($ShortName) {
    # Use provided short name, just clean it up
    $branchSuffix = ConvertTo-CleanBranchName -Name $ShortName
} else {
    # Generate from description with smart filtering
    $branchSuffix = Get-BranchName -Description $featureDesc
}

if (-not $branchSuffix) {
    [Console]::Error.WriteLine("[specify] Warning: Feature name is empty after removing unsupported characters. Use -ShortName with letters or digits (for example, user-auth).")
}


# Warn if -Number and -Timestamp are both specified.
if ($Timestamp -and $hasNumber) {
    [Console]::Error.WriteLine("[specify] Warning: -Number is ignored when -Timestamp is used")
    $Number = ''
}

# Determine branch prefix
if ($Timestamp) {
    $featureNum = Get-Date -Format 'yyyyMMdd-HHmmss'
    $branchName = "$featureNum-$branchSuffix"
} else {
    # Determine branch number from existing feature directories. Auto-detect only
    # when -Number was not supplied; an explicit value (including 0) is honored,
    # matching the bash twin's `[ -z "$BRANCH_NUMBER" ]` check.
    [long]$resolvedNumber = 0
    if (-not $hasNumber) {
        $highestNumber = Get-HighestNumberFromSpecs -SpecsDir $specsDir
        if ($highestNumber -eq [long]::MaxValue) {
            Write-Error "Error: feature number must be between 0 and $([long]::MaxValue), got '9223372036854775808'"
            exit 1
        }
        $resolvedNumber = $highestNumber + 1
    } elseif ($Number -notmatch '^[0-9]+$') {
        Write-Error "Error: -Number must be an unsigned integer, got '$Number'"
        exit 1
    } elseif (-not [long]::TryParse($Number, [ref]$resolvedNumber)) {
        Write-Error "Error: -Number must be between 0 and $([long]::MaxValue), got '$Number'"
        exit 1
    }

    $featureNum = ('{0:000}' -f $resolvedNumber)

    # Treat an explicit number as a preference when its prefix is already used
    # by a feature directory. Auto-detected numbers are already conflict-free.
    $specConflict = $false
    if ($hasNumber -and (Test-Path -LiteralPath $specsDir -PathType Container)) {
        $requestedBranchName = Get-FittedBranchName -FeatureNum $featureNum -BranchSuffix $branchSuffix
        $requestedDir = Join-Path $specsDir $requestedBranchName
        if (-not $AllowExistingBranch -or -not (Test-Path -LiteralPath $requestedDir -PathType Container)) {
            $specConflict = Test-SpecPrefixInUse -SpecsDir $specsDir -FeatureNum $featureNum
        }
    }

    if ($specConflict) {
        $requestedNum = $featureNum
        $highestNumber = Get-HighestNumberFromSpecs -SpecsDir $specsDir
        $resolvedNumber = $highestNumber
        do {
            if ($resolvedNumber -eq [long]::MaxValue) {
                Write-Error "Error: feature number must be between 0 and $([long]::MaxValue), got '9223372036854775808'"
                exit 1
            }
            $resolvedNumber++
            $featureNum = ('{0:000}' -f $resolvedNumber)
        } while (Test-SpecPrefixInUse -SpecsDir $specsDir -FeatureNum $featureNum)
        [Console]::Error.WriteLine("[specify] Warning: -Number $requestedNum conflicts with an existing spec directory; using $featureNum instead")
    }

}

# GitHub enforces a 244-byte limit on branch names
# Validate and truncate if necessary
$originalBranchName = "$featureNum-$branchSuffix"
$branchName = Get-FittedBranchName -FeatureNum $featureNum -BranchSuffix $branchSuffix
if ($branchName -ne $originalBranchName) {
    [Console]::Error.WriteLine("[specify] Warning: Branch name exceeded GitHub's 244-byte limit")
    [Console]::Error.WriteLine("[specify] Original: $originalBranchName ($([System.Text.Encoding]::UTF8.GetByteCount($originalBranchName)) bytes)")
    [Console]::Error.WriteLine("[specify] Truncated to: $branchName ($([System.Text.Encoding]::UTF8.GetByteCount($branchName)) bytes)")
}

$featureDir = Resolve-StoragePath $storage (Join-Path $specsDir $branchName)
$specFile = Resolve-StoragePath $storage (Join-Path $featureDir 'spec.md')

if (-not $DryRun) {
    $needsSpec = -not (Test-Path -PathType Leaf $specFile)
    $content = $null
    if ($needsSpec) {
        $content = Resolve-TemplateContent -TemplateName 'spec-template' -RepoRoot $repoRoot
    }

    # Claim the directory exclusively before any write. An auto-numbered run
    # loses when any other directory has its prefix, removes its own empty
    # directory, and retries with a new prefix.
    $autoNumbered = ($Timestamp -or -not $hasNumber) -and -not $AllowExistingBranch
    $attempt = 1
    while ($true) {
        try {
            New-Item -ItemType Directory -Path $featureDir -ErrorAction Stop | Out-Null
        } catch {
            if (-not (Test-Path -LiteralPath $featureDir)) {
                throw
            }
            if ($AllowExistingBranch -and (Test-Path -LiteralPath $featureDir -PathType Container)) {
                break
            }
            if ($Timestamp) {
                Write-Error "Error: Feature directory '$featureDir' already exists. Rerun to get a new timestamp or use a different -ShortName."
            } else {
                Write-Error "Error: Feature directory '$featureDir' already exists. Please use a different feature name or specify a different number with -Number."
            }
            exit 1
        }
        if (-not $autoNumbered -or -not (Test-SpecPrefixInUse -SpecsDir $specsDir -FeatureNum $featureNum -OwnName $branchName)) {
            break
        }
        Remove-Item -LiteralPath $featureDir
        if ($attempt -ge 20) {
            [Console]::Error.WriteLine('ERROR: Could not reserve a feature directory after 20 tries.')
            exit 1
        }
        $attempt++
        Start-Sleep -Milliseconds (Get-Random -Maximum 1000)
        if ($Timestamp) {
            $lostNum = $featureNum
            $featureNum = Get-Date -Format 'yyyyMMdd-HHmmss'
            while ([string]::CompareOrdinal($featureNum, $lostNum) -le 0) {
                Start-Sleep -Milliseconds 100
                $featureNum = Get-Date -Format 'yyyyMMdd-HHmmss'
            }
        } else {
            $highestNumber = Get-HighestNumberFromSpecs -SpecsDir $specsDir
            if ($highestNumber -eq [long]::MaxValue) {
                Write-Error "Error: feature number must be between 0 and $([long]::MaxValue), got '9223372036854775808'"
                exit 1
            }
            $featureNum = ('{0:000}' -f ($highestNumber + 1))
        }
        $branchName = Get-FittedBranchName -FeatureNum $featureNum -BranchSuffix $branchSuffix
        $featureDir = Resolve-StoragePath $storage (Join-Path $specsDir $branchName)
        $specFile = Resolve-StoragePath $storage (Join-Path $featureDir 'spec.md')
    }

    if ($needsSpec) {
        if ($null -ne $content) {
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($specFile, $content, $utf8NoBom)
        } else {
            # Match the bash twin (create-new-feature.sh): warn on stderr that no
            # spec template was found before creating an empty spec file, so the
            # missing-template signal is not silently swallowed on Windows.
            [Console]::Error.WriteLine("Warning: Spec template not found; created empty spec file")
            New-Item -ItemType File -Path $specFile -Force | Out-Null
        }
    }

    # Only automatic projects save the active feature in the selected storage record.
    # Context projects keep it as it is, and SPECIFY_FEATURE_NO_PERSIST always wins.
    if (Test-PersistFeatureSelection -Mode $selectionMode) {
        Save-FeatureJson -RepoRoot $repoRoot -FeatureDirectory $featureDir
    }

    # Set environment variables for the current session
    $env:SPECIFY_FEATURE = $branchName
    $env:SPECIFY_FEATURE_DIRECTORY = $featureDir

    $quotedBranchName = "'" + $branchName.Replace("'", "''") + "'"
    $quotedFeatureDir = "'" + $featureDir.Replace("'", "''") + "'"
    $featureAssignment = '$env:SPECIFY_FEATURE = ' + $quotedBranchName
    $directoryAssignment = '$env:SPECIFY_FEATURE_DIRECTORY = ' + $quotedFeatureDir
    [Console]::Error.WriteLine("# To select this feature: $directoryAssignment")
    [Console]::Error.WriteLine("# Optional label:         $featureAssignment")
}

if ($Json) {
    $obj = [PSCustomObject]@{
        BRANCH_NAME = $branchName
        SPEC_FILE = $specFile
        FEATURE_NUM = $featureNum
    }
    if ($DryRun) {
        $obj | Add-Member -NotePropertyName 'DRY_RUN' -NotePropertyValue $true
    }
    $obj | ConvertTo-Json -Compress
} else {
    Write-Output "BRANCH_NAME: $branchName"
    Write-Output "SPEC_FILE: $specFile"
    Write-Output "FEATURE_NUM: $featureNum"
    if (-not $DryRun) {
        Write-Output "# To select this feature: $directoryAssignment"
        Write-Output "# Optional label:         $featureAssignment"
    }
}

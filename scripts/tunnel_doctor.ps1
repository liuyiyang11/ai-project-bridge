[CmdletBinding()]
param(
    [string]$TunnelClientPath = "tunnel-client",
    [string]$PythonExecutable = "",
    [string]$ConfigPath = "",
    [string]$ProfilePath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))

function Resolve-RepositoryPath {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $Value))
}

function Resolve-Executable {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ([System.IO.Path]::IsPathRooted($Value) -or $Value.Contains("\") -or $Value.Contains("/")) {
        if (-not (Test-Path -LiteralPath $Value -PathType Leaf)) {
            throw "$Label was not found"
        }
        return [System.IO.Path]::GetFullPath($Value)
    }

    $command = Get-Command -Name $Value -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "$Label was not found"
    }
    return $command.Source
}

function Stop-WithFailure {
    param(
        [Parameter(Mandatory = $true)][string]$Layer,
        [Parameter(Mandatory = $true)][string]$Message
    )

    Write-Output "FAILURE_LAYER=$Layer"
    [Console]::Error.WriteLine($Message)
    exit 2
}

function Sanitize-Diagnostics {
    param([AllowEmptyString()][string]$Text)

    $safe = $Text
    $runtimeKey = [Environment]::GetEnvironmentVariable("CONTROL_PLANE_API_KEY")
    if (-not [string]::IsNullOrEmpty($runtimeKey)) {
        $safe = $safe.Replace($runtimeKey, "[redacted]")
    }
    $safe = [regex]::Replace(
        $safe,
        '(?i)(\bAuthorization\b\s*:\s*Bearer\s+)[^\s,;]+',
        '${1}[redacted]'
    )
    $safe = [regex]::Replace(
        $safe,
        '(?i)(\bAuthorization\b\s*[:=]\s*)(?!Bearer\b)[^\s,;]+',
        '${1}[redacted]'
    )
    $safe = [regex]::Replace(
        $safe,
        '(?i)(\b(?:api[_-]?key|admin[_-]?key|bearer|token)\b\s*[:=]\s*)[^\s,;]+',
        '${1}[redacted]'
    )
    $safe = [regex]::Replace($safe, '(?i)(\bBearer\s+)[^\s,;]+', '${1}[redacted]')
    $safe = [regex]::Replace($safe, '(?i)\bsk-[A-Za-z0-9_-]{8,}\b', '[redacted]')
    return $safe.Substring(0, [Math]::Min($safe.Length, 12000))
}

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $PythonExecutable = [Environment]::GetEnvironmentVariable("BRIDGE_MCP_PYTHON")
}
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $PythonExecutable = "python"
}
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = [Environment]::GetEnvironmentVariable("BRIDGE_MCP_CONFIG")
}
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = "config.local.yaml"
}
if ([string]::IsNullOrWhiteSpace($ProfilePath)) {
    $ProfilePath = [Environment]::GetEnvironmentVariable("BRIDGE_TUNNEL_PROFILE")
}
if ([string]::IsNullOrWhiteSpace($ProfilePath)) {
    $ProfilePath = ".bridge\tunnel-profile.yaml"
}

try {
    $pythonCommand = Resolve-Executable $PythonExecutable "Python executable"
} catch {
    Stop-WithFailure "LOCAL_MCP_START" $_.Exception.Message
}

try {
    $tunnelClientCommand = Resolve-Executable $TunnelClientPath "tunnel-client executable"
} catch {
    Stop-WithFailure "CONTROL_PLANE" $_.Exception.Message
}

$configAbsolutePath = Resolve-RepositoryPath $ConfigPath
$profileAbsolutePath = Resolve-RepositoryPath $ProfilePath

if (-not (Test-Path -LiteralPath $configAbsolutePath -PathType Leaf)) {
    Stop-WithFailure "LOCAL_MCP_START" "Bridge config was not found"
}
if (-not (Test-Path -LiteralPath $profileAbsolutePath -PathType Leaf)) {
    Stop-WithFailure "LOCAL_MCP_START" "Tunnel profile was not found"
}

$runtimeKey = [Environment]::GetEnvironmentVariable("CONTROL_PLANE_API_KEY")
if ([string]::IsNullOrWhiteSpace($runtimeKey)) {
    Stop-WithFailure "TUNNEL_AUTH" "CONTROL_PLANE_API_KEY is unset"
}

$tunnelId = [Environment]::GetEnvironmentVariable("CONTROL_PLANE_TUNNEL_ID")
if ([string]::IsNullOrWhiteSpace($tunnelId)) {
    Stop-WithFailure "TUNNEL_AUTH" "CONTROL_PLANE_TUNNEL_ID is unset"
}
if ($tunnelId -notmatch '^tunnel_[0-9a-f]{32}$') {
    Stop-WithFailure "TUNNEL_AUTH" "CONTROL_PLANE_TUNNEL_ID has invalid format"
}

$adminKey = [Environment]::GetEnvironmentVariable("OPENAI_ADMIN_KEY")
if (-not [string]::IsNullOrWhiteSpace($adminKey)) {
    Stop-WithFailure "TUNNEL_AUTH" "Unset OPENAI_ADMIN_KEY before running the runtime doctor"
}

Push-Location $repositoryRoot
try {
    $validationOutput = (& $pythonCommand -m bridge.tunnel_profile validate --profile-path $profileAbsolutePath 2>&1 | Out-String)
    $validationExit = $LASTEXITCODE
    if ($validationExit -ne 0) {
        Stop-WithFailure "LOCAL_MCP_START" "Tunnel profile validation failed"
    }

    $doctorOutput = (& $tunnelClientCommand doctor --profile-file $profileAbsolutePath --explain 2>&1 | Out-String)
    $doctorExit = $LASTEXITCODE
    $safeOutput = Sanitize-Diagnostics $doctorOutput
    if ($doctorExit -ne 0) {
        $layerOutput = ($doctorOutput | & $pythonCommand -m bridge.tunnel_profile classify 2>$null | Out-String).Trim()
        $allowedLayers = @("CONTROL_PLANE", "TUNNEL_AUTH", "LOCAL_MCP_START", "MCP_INITIALIZE", "TOOLS_DISCOVERY", "CODEX_DISCOVERY")
        if ($allowedLayers -notcontains $layerOutput) {
            $layerOutput = "CONTROL_PLANE"
        }
        Write-Output "FAILURE_LAYER=$layerOutput"
        if (-not [string]::IsNullOrWhiteSpace($safeOutput)) {
            [Console]::Error.WriteLine($safeOutput.Trim())
        }
        exit $doctorExit
    }
    if (-not [string]::IsNullOrWhiteSpace($safeOutput)) {
        Write-Output $safeOutput.Trim()
    }
    exit 0
}
finally {
    Pop-Location
}

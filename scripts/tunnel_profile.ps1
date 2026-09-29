[CmdletBinding()]
param(
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
    param([Parameter(Mandatory = $true)][string]$Value)

    if ([System.IO.Path]::IsPathRooted($Value) -or $Value.Contains("\") -or $Value.Contains("/")) {
        if (-not (Test-Path -LiteralPath $Value -PathType Leaf)) {
            throw "Python executable was not found"
        }
        return [System.IO.Path]::GetFullPath($Value)
    }

    $command = Get-Command -Name $Value -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "Python executable was not found"
    }
    return $command.Source
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

$pythonCommand = Resolve-Executable $PythonExecutable
$configAbsolutePath = Resolve-RepositoryPath $ConfigPath
$profileAbsolutePath = Resolve-RepositoryPath $ProfilePath

if (-not (Test-Path -LiteralPath $configAbsolutePath -PathType Leaf)) {
    throw "Bridge config was not found"
}

Push-Location $repositoryRoot
try {
    & $pythonCommand -m bridge.tunnel_profile generate `
        --python-executable $pythonCommand `
        --config-path $configAbsolutePath `
        --profile-path $profileAbsolutePath
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}

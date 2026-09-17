[CmdletBinding()]
param(
    [string]$TunnelClientPath = "tunnel-client",
    [string]$PythonExecutable = "",
    [string]$ConfigPath = "",
    [string]$ProfilePath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$doctorScript = Join-Path $PSScriptRoot "tunnel_doctor.ps1"
& $doctorScript `
    -TunnelClientPath $TunnelClientPath `
    -PythonExecutable $PythonExecutable `
    -ConfigPath $ConfigPath `
    -ProfilePath $ProfilePath
$doctorExit = $LASTEXITCODE
if ($doctorExit -ne 0) {
    exit $doctorExit
}

if ([string]::IsNullOrWhiteSpace($ProfilePath)) {
    $ProfilePath = [Environment]::GetEnvironmentVariable("BRIDGE_TUNNEL_PROFILE")
}
if ([string]::IsNullOrWhiteSpace($ProfilePath)) {
    $ProfilePath = ".bridge\tunnel-profile.yaml"
}
if ([System.IO.Path]::IsPathRooted($ProfilePath)) {
    $profileAbsolutePath = [System.IO.Path]::GetFullPath($ProfilePath)
} else {
    $repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    $profileAbsolutePath = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $ProfilePath))
}

if ([System.IO.Path]::IsPathRooted($TunnelClientPath) -or $TunnelClientPath.Contains("\") -or $TunnelClientPath.Contains("/")) {
    $tunnelClientCommand = $TunnelClientPath
} else {
    $tunnelClient = Get-Command -Name $TunnelClientPath -CommandType Application -ErrorAction Stop
    $tunnelClientCommand = $tunnelClient.Source
}

& $tunnelClientCommand run --profile-file $profileAbsolutePath
exit $LASTEXITCODE

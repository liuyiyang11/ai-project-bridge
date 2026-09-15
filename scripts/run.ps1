param(
  [switch]$Once,
  [string]$Config = "config.local.yaml"
)
$ErrorActionPreference = "Stop"
$Python = 'C:\Users\29833\.conda\envs\py10\python.exe'
if ($Once) {
  & $Python -m bridge --config $Config run-once
} else {
  & $Python -m bridge --config $Config run
}

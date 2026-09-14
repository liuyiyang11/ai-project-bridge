param(
  [switch]$Once,
  [string]$Config = "config.local.yaml"
)
$ErrorActionPreference = "Stop"
$Python = 'E:\anaconda\envs\py39\python.exe'
if ($Once) {
  & $Python -m bridge --config $Config run-once
} else {
  & $Python -m bridge --config $Config run
}

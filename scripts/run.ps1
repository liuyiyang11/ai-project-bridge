param(
  [switch]$Once,
  [string]$Config = "config.local.yaml"
)
$ErrorActionPreference = "Stop"
if ($Once) {
  python -m bridge --config $Config run-once
} else {
  python -m bridge --config $Config run
}

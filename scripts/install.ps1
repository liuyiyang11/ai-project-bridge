$ErrorActionPreference = "Stop"
$Python = 'C:\Users\29833\.conda\envs\py10\python.exe'
& $Python -m pip install -e '.[test]'
Write-Host "Installed ai-project-bridge. Copy config.local.yaml.example to config.local.yaml and edit it privately."


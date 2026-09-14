$ErrorActionPreference = "Stop"
$Python = 'E:\anaconda\envs\py39\python.exe'
& $Python -m pip install -e '.[test]'
Write-Host "Installed ai-project-bridge. Copy config.local.yaml.example to config.local.yaml and edit it privately."


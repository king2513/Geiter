$ErrorActionPreference = "Stop"

$root = Join-Path $PSScriptRoot "workspace"
New-Item -ItemType Directory -Path $root -Force | Out-Null

python -m geiter --root $root init --json
python -m geiter --root $root goal add "Become easy for agents to discover and trust" --json
$prompt = python -m geiter --root $root prompt add "What is Geiter?" --intent discovery --json | ConvertFrom-Json
python -m geiter --root $root observe $prompt.id --provider fixture --answer "Geiter is an agent-native GEO runtime." --citation "https://example.com/geiter" --json
python -m geiter --root $root analyze --json

param(
    [switch]$PushMain
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repo

$branch = "hotfix/v1.0.0-quest-economy"
$existing = git branch --list $branch
if ($existing) {
    git checkout $branch
} else {
    git checkout -b $branch
}

python scripts/verify-v1.0.0.py
python -m compileall -q faervell_npc
python -m pytest -q

git add `
  faervell_npc/main.py `
  faervell_npc/services/quest_rewards.py `
  faervell_npc/services/v100_hotfix.py `
  tests/test_v100_quest_hotfix.py `
  scripts/deploy-hotfix-v1.0.0.sh `
  scripts/push-hotfix-v1.0.0.ps1

$changes = git status --porcelain
if ($changes) {
    git commit -m "Fix quest templates, GM delivery, and OTN rewards"
}
git push -u origin $branch

if ($PushMain) {
    git checkout main
    git pull --ff-only origin main
    git merge --ff-only $branch
    git push origin main
}

Write-Host "Branch pushed: $branch"
if (-not $PushMain) {
    Write-Host "After CI is green run the same script with -PushMain."
}

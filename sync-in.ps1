# Copy this plugin from the standalone repository into the N.E.K.O. source tree
# so it can be loaded and exercised in a real host.
#
# The standalone repository is the source of truth; the in-repo copy is a
# scratch working copy for host testing and is not meant to be committed there.
# Run sync-out.ps1 before committing so edits flow back.
#
# Only the files the host loads are copied (a whitelist, not a blacklist) —
# repository-only files such as .git, .github, .vscode, ruff.toml, pyproject.toml
# and tests/ must not land in the plugin tree.

[CmdletBinding()]
param(
    [string]$RepoDir = $PSScriptRoot,
    [string]$NekoRoot = "E:\NEKOdm\N.E.K.O",
    [switch]$SkipMetadata
)

$ErrorActionPreference = "Stop"

$pluginId = "mcbot_bridge"
$target = Join-Path (Join-Path $NekoRoot "plugin\plugins") $pluginId

$items = @(
    "__init__.py",
    "client.py",
    "service.py",
    "prompts.py",
    "plugin.toml",
    "config.schema.json",
    "README.md",
    "i18n"
)

if (-not (Test-Path (Join-Path $RepoDir "plugin.toml"))) {
    throw "no plugin.toml in '$RepoDir' — run this from the standalone repository root"
}

New-Item -ItemType Directory -Force -Path $target | Out-Null

foreach ($item in $items) {
    $source = Join-Path $RepoDir $item
    if (-not (Test-Path $source)) {
        Write-Warning "skipped missing item: $item"
        continue
    }
    Copy-Item -Path $source -Destination $target -Recurse -Force
    Write-Host "  -> $item"
}

# Stale bytecode from an earlier copy will shadow edits (the classic
# "I changed the code but nothing happened" trap).
$pycache = Join-Path $target "__pycache__"
if (Test-Path $pycache) {
    Remove-Item -Recurse -Force $pycache
}

# plugin.meta.json is a build artifact, not source: it is generated from the
# tree by importing the plugin, which only works once the plugin sits at its
# packaged path (plugin/plugins/<id>). Without it the host falls back to
# scanning the manifest — functional, just a thinner parameter form.
if (-not $SkipMetadata) {
    $meta = Join-Path $target "plugin.meta.json"
    if (Test-Path $meta) {
        Remove-Item -Force $meta
    }
    Push-Location $NekoRoot
    try {
        uv run python -B -c @"
from pathlib import Path
from plugin.neko_plugin_cli.core.metadata_probe import write_packaged_metadata
d = Path('plugin/plugins/$pluginId')
write_packaged_metadata(source_dir=d, target_dir=d)
"@
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "metadata probe failed (exit $LASTEXITCODE); host will fall back to manifest scanning"
        }
    } catch {
        Write-Warning "could not regenerate plugin.meta.json: $_"
    } finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "Synced $pluginId -> $target"
Write-Host "Next: click Reload on the plugin card in N.E.K.O. (restart the plugin"
Write-Host "if you changed plugin.toml rather than only Python code)."

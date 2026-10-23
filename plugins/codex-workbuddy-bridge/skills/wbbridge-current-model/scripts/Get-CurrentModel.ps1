<#
.SYNOPSIS
    Resolve the model Codex is ACTUALLY running against, and the config default.

.DESCRIPTION
    Two different questions, two different sources:

      "right now"        -> the local proxy endpoint /wb/last-model. The proxy is
                            the only component that sees the real `model` field on
                            the wire, so it is the authority.
      "new sessions"     -> config.toml `model` key.

    Switching the model inside a Codex session (desktop picker) is a runtime
    override that is NEVER written back to config.toml, so a config-only read
    reports a stale default. This script reports both and flags the mismatch.

.NOTES
    Keep this file pure ASCII. Windows PowerShell 5.1 reads .ps1 as ANSI/GBK, so
    non-ASCII comments turn into mojibake and break parsing.
#>
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$CatalogPath,
    [int]$ProxyPort = 8788,
    [switch]$Json
)

function Find-Config {
    if ($ConfigPath -and (Test-Path $ConfigPath)) { return $ConfigPath }
    $homeDir = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
    $c = Join-Path $homeDir "config.toml"
    if (Test-Path $c) { return $c }
    return $null
}

function Parse-TomlKeyValue {
    param([string]$Path)
    $map = @{}
    foreach ($line in (Get-Content $Path -Encoding UTF8)) {
        $s = $line.Trim()
        if ($s -eq '' -or $s.StartsWith('#')) { continue }
        if ($s -match '^(?<k>[A-Za-z0-9_.\-]+)\s*=\s*(?<v>.*)$') {
            $k = $Matches['k']
            $v = $Matches['v'].Trim()
            $v = $v -replace '\s+#.*$', ''
            if ($v -match '^"(?<str>.*)"$') { $v = $Matches['str'] }
            elseif ($v -match "^'(?<str>.*)'$") { $v = $Matches['str'] }
            $map[$k] = $v
        }
    }
    return $map
}

function Load-Catalog {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path $Path)) { return $null }
    try {
        $raw = Get-Content $Path -Raw -Encoding UTF8
        $obj = $raw | ConvertFrom-Json
        if ($obj -and $obj.models) { return $obj.models }
    } catch { }
    return $null
}

function Get-LiveModel {
    # Ask the proxy which model it last saw on the wire. Authority for "now".
    param([int]$Port = 8788)
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/wb/last-model" -TimeoutSec 5 -ErrorAction Stop
        if ($r -and $r.model) {
            $when = $null
            if ($r.ts) {
                try { $when = (Get-Date -UnixTimeSeconds $r.ts).ToString('HH:mm:ss') } catch { $when = $null }
            }
            return [pscustomobject]@{
                model = $r.model
                ts    = $r.ts
                count = $r.count
                when  = $when
            }
        }
    } catch { }
    return $null
}

$configFile = Find-Config
if (-not $configFile) {
    Write-Error "Could not locate a Codex config.toml (set -ConfigPath explicitly)."
    exit 2
}

$cfg = Parse-TomlKeyValue -Path $configFile
$modelSlug = if ($cfg.ContainsKey('model')) { $cfg['model'] } else { $null }
$provider = if ($cfg.ContainsKey('model_provider')) { $cfg['model_provider'] } else { $null }
$reasoningEffort = if ($cfg.ContainsKey('model_reasoning_effort')) { $cfg['model_reasoning_effort'] } else { $null }

if (-not $modelSlug) {
    Write-Error "No 'model' key found in $configFile."
    exit 3
}

# Resolve catalog from config
$catFile = $CatalogPath
if (-not $catFile -and $cfg.ContainsKey('model_catalog_json')) {
    $catFile = $cfg['model_catalog_json']
    if ($catFile -and -not (Test-Path $catFile)) {
        $catFile = Join-Path (Split-Path $configFile) $catFile
    }
}
$models = Load-Catalog -Path $catFile

# Authority first: what is the proxy actually seeing?
$live = Get-LiveModel -Port $ProxyPort

$result = [ordered]@{
    config_default  = $modelSlug
    provider        = $provider
    reasoning_effort = $reasoningEffort
    from_config     = $configFile
    catalog_path    = if ($catFile) { $catFile } else { $null }
}

if ($live) {
    $result.effective_model   = $live.model
    $result.effective_source  = 'proxy-observed'
    $result.last_request_time = $live.when
    $result.requests_observed = $live.count
    if ($live.model -ne $modelSlug) {
        $result.config_default_mismatch = $true
        $result.note = "Session switched to '$($live.model)'; config.toml '$modelSlug' is only the default for new sessions."
    }
} else {
    $result.effective_model  = $modelSlug
    $result.effective_source = 'config-only (proxy down or no request yet)'
}

# Enrich from the catalog using the EFFECTIVE slug
$effSlug = $result.effective_model
$effModel = $null
if ($models -and $effSlug) {
    $effModel = $models | Where-Object { $_.slug -eq $effSlug } | Select-Object -First 1
}
$result.catalog_found = ($null -ne $effModel)
if ($effModel) {
    $result.effective_display_name = if ($effModel.display_name) { $effModel.display_name } else { $effSlug }
    $result.context_window    = $effModel.context_window
    $result.max_output_tokens = $effModel.max_output_tokens
    if ($effModel.supported_reasoning_levels) {
        $result.supported_reasoning_levels = ($effModel.supported_reasoning_levels | ForEach-Object { $_.effort })
    }
} else {
    $result.effective_display_name = $effSlug
    if ($catFile -and -not (Test-Path $catFile)) {
        $result.catalog_warning = "catalog file not found: $catFile"
    } elseif ($catFile) {
        $result.catalog_warning = "slug '$effSlug' not present in catalog"
    }
}

# Note: deliberately NO cmdline-override detection. Scanning every codex.exe for a
# -m/--model flag picks up unrelated processes (CLI runs, other sessions) and
# reports them as "the override", which is misleading. The proxy endpoint above is
# the authority; config is the default. Anything else is noise.

if ($Json) {
    $result | ConvertTo-Json -Compress
} else {
    Write-Host "In use now : $($result.effective_display_name)  (slug: $effSlug)"
    Write-Host "  source           : $($result.effective_source)"
    if ($result.last_request_time) {
        Write-Host "  last request     : $($result.last_request_time)  (request #$($result.requests_observed) seen by proxy)"
    }
    Write-Host "  provider         : $provider"
    if ($result.context_window)    { Write-Host ("  context window   : {0}" -f $result.context_window) }
    if ($result.max_output_tokens) { Write-Host ("  max output tokens: {0}" -f $result.max_output_tokens) }
    if ($reasoningEffort)          { Write-Host "  reasoning effort : $reasoningEffort" }
    Write-Host "  config default   : $modelSlug   (new sessions only)"
    if ($result.config_default_mismatch) {
        Write-Host "  !! mismatch: config says '$modelSlug' but the wire says '$effSlug'"
    }
    if ($result.catalog_found) { Write-Host "  catalog          : resolved" }
    else { Write-Host "  catalog          : NOT resolved $(if($result.catalog_warning){'('+$result.catalog_warning+')'})" }
    if ($result.cmdline_override) { Write-Host "  cmdline override : $($result.cmdline_override)" }
}

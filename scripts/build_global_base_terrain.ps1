Param(
  [Parameter(Mandatory = $true)]
  [string]$DemDir,

  [Parameter(Mandatory = $false)]
  [int]$MaxZoom = 8,

  [Parameter(Mandatory = $false)]
  [string]$OutDir = ".\\downloads\\terrain\\base_z8"
)

$ErrorActionPreference = "Stop"

try {
  if (-not (Test-Path -LiteralPath $DemDir)) {
    throw "DemDir not found: $DemDir"
  }

  New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

  $vrtPath = Join-Path $OutDir "global.vrt"
  if (Test-Path -LiteralPath $vrtPath) {
    Remove-Item -LiteralPath $vrtPath -Force
  }

  & gdalbuildvrt $vrtPath (Join-Path $DemDir "*.tif")
  if ($LASTEXITCODE -ne 0) { throw "gdalbuildvrt failed with exit code $LASTEXITCODE" }

  & ctb-tile -f Mesh -C -N -l -o $OutDir $vrtPath -z $MaxZoom
  if ($LASTEXITCODE -ne 0) { throw "ctb-tile failed with exit code $LASTEXITCODE" }
}
catch {
  throw
}


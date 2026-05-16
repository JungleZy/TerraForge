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

  # Use cesiumlab_terrain.py as the source of truth.
  # Note: Requires a Python env with numpy + GDAL bindings available on PATH.
  & python -m services.terrain_tiling.cesiumlab_terrain -i $DemDir -o $OutDir --max-level $MaxZoom
  if ($LASTEXITCODE -ne 0) { throw "cesiumlab_terrain build failed with exit code $LASTEXITCODE" }
}
catch {
  throw
}

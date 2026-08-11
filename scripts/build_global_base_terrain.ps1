Param(
  [Parameter(Mandatory = $true)]
  [string]$DemDir,

  [Parameter(Mandatory = $false)]
  [int]$MaxZoom = 8,

  [Parameter(Mandatory = $false)]
  [string]$OutDir = ".\\downloads\\terrain\\base_z8",

  [Parameter(Mandatory = $false)]
  [int]$TileSize = 65
)

$ErrorActionPreference = "Stop"

try {
  if (-not (Test-Path -LiteralPath $DemDir)) {
    throw "DemDir not found: $DemDir"
  }

  New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

  # Use cesium_terrain.py as the source of truth.
  #
  # 模块路径必须带 src. 前缀 —— src-layout 迁移后代码在 src/services/ 下，
  # 此前这里还是 `services.terrain_tiling...`，跑起来直接 ModuleNotFoundError，
  # 而且没有任何测试守着（tests/test_build_scripts_contract.py 现在钉住了）。
  #
  # --tile-size 65 与应用侧一致（dem_task_tiler.TileParams.tile_size = 65）。
  # 走 CLI 默认值 17 的话，base 的顶点网格每轴比子层稀疏 4 倍，级联切换时
  # 会看到明显的几何精度跳变。
  #
  # 用 uv run 而不是裸 python：项目统一用 uv 管理 venv（见 CLAUDE.md），
  # 裸 python 要求调用者自己先激活装好 numpy+GDAL 的环境。
  & uv run python -m src.services.terrain_tiling.cesium_terrain `
      -i $DemDir -o $OutDir --max-level $MaxZoom --tile-size $TileSize
  if ($LASTEXITCODE -ne 0) { throw "cesium_terrain build failed with exit code $LASTEXITCODE" }
}
catch {
  throw
}

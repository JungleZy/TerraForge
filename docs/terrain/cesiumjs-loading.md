# CesiumJS 加载 Quantized-Mesh Terrain 示例

## 1) Base terrain provider

```js
const baseTerrain = await Cesium.CesiumTerrainProvider.fromUrl(
  "http://localhost:5000/terrain/base/layer.json"
);
```

## 2) Local DEM overlay provider

```js
const localDemOverlay = await Cesium.CesiumTerrainProvider.fromUrl(
  "http://localhost:5000/terrain/dem/1/layer.json"
);
```

## 3) 说明

如果 local 的 `layer.json` 里包含 `parentUrl` 指向 base，那么只需要加载 local 的 provider，并把它传给 `Viewer`（例如 `new Cesium.Viewer("cesiumContainer", { terrainProvider: localDemOverlay })`）即可；Cesium 会按 `parentUrl` 自动级联到 base。


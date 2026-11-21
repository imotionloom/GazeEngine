# Vendored JS dependencies

These files are copied verbatim from the npm packages below so the VRM viewer runs **offline**
and on machines that cannot reach a CDN. Nothing here is modified; the import map in
`3d/index.html` points at these paths instead of jsdelivr.

| Path | Package | Version | License |
|---|---|---|---|
| `three/build/three.module.js` | [three](https://www.npmjs.com/package/three) | 0.160.0 | MIT |
| `three/examples/jsm/loaders/GLTFLoader.js` | three | 0.160.0 | MIT |
| `three/examples/jsm/utils/BufferGeometryUtils.js` | three | 0.160.0 | MIT |
| `three-vrm/three-vrm.module.js` | [@pixiv/three-vrm](https://www.npmjs.com/package/@pixiv/three-vrm) | 3.x | MIT |

Only the files actually reached by `import` are vendored (three → GLTFLoader →
BufferGeometryUtils, plus three-vrm). Everything else lives in the CDN if you want it.

To upgrade, replace the files and keep the same relative layout; the `three/addons/` prefix in
the import map is what makes `import … from 'three/addons/loaders/GLTFLoader.js'` resolve.

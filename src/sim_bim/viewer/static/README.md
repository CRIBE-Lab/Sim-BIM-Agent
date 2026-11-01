# Vendored three.js

`three.min.js`, `OrbitControls.js` and `GLTFLoader.js` are the UMD builds of three.js
r0.137.5 (`build/three.min.js`, `examples/js/controls/OrbitControls.js`,
`examples/js/loaders/GLTFLoader.js` from https://unpkg.com/three@0.137.5/).
License: MIT, Copyright 2010-2022 three.js authors.

The UMD builds attach to the global `THREE`, so the viewer works from `file://` without a
module loader. Later three.js releases ship ES modules only.

## What changed

<!-- One or two sentences. Link the issue if there is one. -->

## Which layer

- [ ] `p1_perception`
- [ ] `p2_decision`
- [ ] `p3_exec`
- [ ] renderer `3d/` or `tools/`
- [ ] docs only

## Checks

- [ ] `pytest tests -q` — expect `78 passed, 5 skipped` when the model weights are absent
- [ ] `node tests/js_smoke.mjs 3d/index.html` if the renderer changed
- [ ] No new absolute paths, no local credentials, nothing from `models/`, `3d/*.vrm` or
      `test_videos/*.mp4` added to the tree

## Behaviour changes

If this alters gaze behaviour, say what you expect to see differently on a real capture,
and attach or describe the trajectory CSV you compared. Parameter-only changes belong in
`p2_decision/config.py` — mention the old and new values.

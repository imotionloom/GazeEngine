# Trajectory index

Numeric per-frame logs of real capture sessions (no imagery). Columns:
`frame, ts_ms, n_faces, active_track, state, preset, neck_pan, neck_tilt, eye_pan, eye_tilt, offset, lid, source, speaking`

| File | Frames | Duration | FPS | Faces | States | Presets |
|---|---|---|---|---|---|---|
| `03d04718ae0ee72fe632cb5c0da6d16c.csv` | 900 | 30.6s | 29 | 0–19 | LOOKING_AT_PERSON:641, SCANNING:121, GAZE_AWAY:85 | idle:900 |
| `07f5ec6588b9e7fc8532e8e452ad3133.csv` | 666 | 21.9s | 30 | 0–4 | SCANNING:514, LOOKING_AT_PERSON:152 | idle:666 |
| `119c118ccdb3adcfd2c54031f8f3cc37.csv` | 675 | 10.8s | 62 | 0–2 | LOOKING_AT_PERSON:552, GAZE_AWAY:71, SCANNING:32 | idle:675 |
| `143b111d7d18dd6c6f8f89390c08f72c.csv` | 230 | 7.6s | 30 | 24–46 | LOOKING_AT_PERSON:212, SCANNING:18 | idle:230 |
| `3558aa8dd6c5c6aa8c6eb4ade9497187.csv` | 900 | 29.7s | 30 | 1–8 | LOOKING_AT_PERSON:784, RETURN:55, SCANNING:35 | idle:900 |
| `399c4a7a4fb100d14ca6296b792b3565.csv` | 900 | 29.7s | 30 | 0–1 | SCANNING:822, LOOKING_AT_PERSON:78 | idle:900 |
| `3e17d45820070b27d13a420d06ed3f7a.csv` | 900 | 29.7s | 30 | 0–9 | LOOKING_AT_PERSON:534, SCANNING:152, RETURN:110 | idle:900 |
| `49e1a2612552ccc0ee187152fbb54bfe.csv` | 119 | 3.9s | 30 | 0–4 | SCANNING:119 | idle:119 |
| `4d6872b037f75e9702bc93afe730f9f3.csv` | 354 | 11.6s | 30 | 0–0 | SCANNING:354 | idle:354 |
| `78d2c2a73e3d2d6ad33d80d82ed571cd.csv` | 900 | 29.7s | 30 | 0–2 | LOOKING_AT_PERSON:799, RETURN:55, GAZE_AWAY:28 | idle:900 |
| `7ad03260147b1703ee03ece31b1ace72.csv` | 900 | 29.7s | 30 | 0–2 | SCANNING:900 | idle:900 |
| `80aa4596af2e4eea463a8b1e09d4296a.csv` | 900 | 29.7s | 30 | 0–1 | SCANNING:689, LOOKING_AT_PERSON:211 | idle:900 |
| `a1e8c3106e65bb015febe8969ff74ff4.csv` | 247 | 10.1s | 24 | 0–3 | SCANNING:247 | idle:247 |
| `bb127ba972c5de2ce55e94be2195e3b3.csv` | 529 | 17.4s | 30 | 0–9 | SCANNING:377, LOOKING_AT_PERSON:152 | idle:529 |
| `bbe32e8c051f96a3ac4b6a0259eaf5e3.csv` | 392 | 12.9s | 30 | 0–12 | SCANNING:262, LOOKING_AT_PERSON:130 | idle:392 |
| `c2fbd88ed1181897883f07dd5597153b.csv` | 183 | 6.0s | 30 | 0–1 | SCANNING:146, LOOKING_AT_PERSON:37 | idle:183 |
| `ceb674ad5a0b2805d5c0c38271afeb6b.csv` | 403 | 13.3s | 30 | 0–3 | LOOKING_AT_PERSON:202, SCANNING:201 | idle:403 |

Regression use: replay a baseline CSV and diff it against your build (`python test_videos/replay_server.py`, then open `replay.html`). A behavior change that shifts the state mix or the angle envelope shows up here before it shows up in a video.

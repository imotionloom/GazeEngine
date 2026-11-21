# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Use GitHub's private reporting flow instead:
[**Report a vulnerability**](https://github.com/imotionloom/GazeEngine/security/advisories/new)
(Security tab → Advisories → Report a vulnerability). That opens a private thread visible
only to you and the maintainers.

What helps us reproduce it:

- Affected file / component (`p1_perception`, `p2_decision`, `p3_exec`, `tools/`, `3d/`)
- Whether the issue needs a webcam, a model weight, or a specific OS
- Steps or a minimal script; a packet capture if it is a network issue
- What an attacker could achieve, and what access they need first

We aim to acknowledge within a few days and to publish an advisory once a fix ships.
This is a hobby-scale project with no SLA, so please plan around that.

## Scope

This repository ships a PC-side research prototype. Things that are **in scope**:

- Code execution, path traversal or file write through the local HTTP/WS tools
  (`tools/action_editor`, `tools/calib_bridge`, `tools/ws_bridge`, `3d/`)
- Denial of service or crash caused by crafted input (video, CSV trajectory, ZMQ payload,
  protobuf message) — the FSM and calibration paths are the interesting ones
- Model or data handling that leaks local files (`models/`, `test_videos/`)
- Dependency vulnerabilities in `requirements.txt`

Things that are **known and by design**, so not worth a report:

- All services bind `127.0.0.1` only; they are not hardened for exposure to a LAN or the
  internet, and the action editor can rewrite `actions.py` on purpose
- `p3_exec/` and the RKNN path in `p1_perception/main.py` are stubs — no hardware is driven
- There is no authentication anywhere. Do not put this on a public interface.

## Out of scope

Third-party model weights, VRM avatars and test footage are not distributed with this
repository; report issues about them upstream (`models/README.md` lists the sources).

# Third-Party Notices

The controller contains code adapted from the Docker scale-set example in
[actions/scaleset](https://github.com/actions/scaleset) at commit
`9b2803251ede77816509dcd90aadc0690dd80763`.

Copyright (c) GitHub, Inc. and contributors. That adapted material is licensed
under the MIT License. Its local attribution is also preserved in
`controller/NOTICE`.

The project interoperates with:

- GitHub Actions Runner: <https://github.com/actions/runner> — MIT License
- GitHub Actions Runner Scale Set Client: <https://github.com/actions/scaleset> — MIT License
- Docker Engine and Docker Compose: <https://github.com/docker> — licenses vary by component

The original code in this repository is released under the Unlicense as stated
in `LICENSE`. The Unlicense does not replace third-party license notices.

`deploy/seccomp/chromium-v1.json` is derived from Playwright's
`utils/docker/seccomp_profile.json` at commit
`39e3553a4f283a41134d75d7e404484bd9e6865a`, with `chroot` added to the allowed
system calls for Chromium's sandbox. Copyright Microsoft Corporation and
contributors. The source is licensed under Apache License 2.0; the license is
preserved in `deploy/seccomp/LICENSE`. This third-party profile remains under
that license rather than the Unlicense.

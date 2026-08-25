# Third-party notices

The root MIT license covers ZhenxiangAI original files and files explicitly released under MIT. It does not relicense the complete runtime stack, third-party source, platform services, or content.

## ZhenxiangAI transparent derivative core

- Upstream project: <https://github.com/yt-dlp/yt-dlp>
- Upstream release: `2026.07.04`
- Audited commit: `fdec00e0bf530dc6c3cc7b1dd780e95d9ae460e9`
- Upstream project credits: pukkandan, current yt-dlp maintainers, and contributors
- License: Unlicense

A pinned derivative runtime subset is distributed under `vendor/transparent-core/yt_dlp/`. It includes only the Bilibili, Xiaohongshu, TikTok/Douyin, and unsupported-URL Generic fallback extractors plus their required runtime support; all other upstream extractors are omitted. The original `LICENSE`, `UPSTREAM.md`, and `THIRD_PARTY_LICENSES.txt` are retained. ZhenxiangAI maintains the product integration and derivative snapshot; the vendored upstream source is not represented as ZhenxiangAI original work.

## wx_channels_download

- Upstream source: <https://github.com/ltaoo/wx_channels_download>
- ZhenxiangAI fork: <https://github.com/Zhenxiangai/wx_channels_download>
- Audited fork revision: `0b99743cb6d7eab91273e7d669c5e1fe55508a02`
- Downloaded release: `v260810-zhenxiangai.3`
- Release archive SHA-256: `54f54ce3f65def9ae922dea5892a77c78aaeec2c67f1aa295204393d71c05dba`
- Binary SHA-256: `fddf28b5327690f0164bf905294784288495b1322d759bbc6a24120c82a5da37`
- License: MIT with Commons Clause License Condition v1.0

The repository does not bundle this source or binary; bootstrap downloads the fixed ZhenxiangAI fork release. That release is built from the audited fork revision while preserving the upstream copyright and license. The Commons Clause restricts selling software whose value derives substantially from this component; obtain separate permission when commercial use may fall within that condition.

## whisper.cpp

- Source: <https://github.com/ggml-org/whisper.cpp>
- Verified release: `v1.9.2`
- Verified commit: `306c88f4d1286aec1bf96e544632897886af5501`
- License: MIT

The project invokes an independently installed `whisper-cli`; it does not bundle the binary or source.

## FFmpeg

- Source: <https://ffmpeg.org/>
- Verified upstream release: `n8.1.2`
- Verified upstream commit: `38b88335f99e76ed89ff3c93f877fdefce736c13`
- Verified Homebrew formula build: `8.1.2_1`
- Locally applicable license: GPL-3.0-or-later because the verified build enables GPL components

The project invokes an independently installed `ffmpeg`. Anyone distributing an FFmpeg binary must comply with that binary's build-specific licenses.

## Whisper model weights

- File: `ggml-small.bin`
- Source revision: `c521a4b02f422512d734391fdf08bb08c0862f68`
- SHA-256: `1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b`
- Source license: OpenAI MIT

Bootstrap downloads and verifies the model. Model weights are not committed or included in a Release.

## Bilibili runtime services

The Bilibili API/CDN route is a runtime platform interface, not an official or open API grant. Bilibili terms, robots policy, account permissions, and content rights continue to apply. Access to an endpoint and the transparent core's Unlicense do not grant rights to automate retrieval, republish, distribute, or commercially use platform content.

## Distribution boundary

Source releases may include ZhenxiangAI original code, the transparent derivative source, its real licenses, and these notices. They exclude FFmpeg, whisper.cpp, model weights, the Channels backend binary, Cookies, account/login state, certificates, proxy configuration, and real archive content.

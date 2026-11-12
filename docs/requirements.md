# Packager requirements

## Segments

- Each video rendition is fragmented CMAF (init `init.mp4` + `$Number$.m4s` or time-addressed segments).
- Segment boundaries must fall on video IDR/sync samples. For the bundled fixtures, GOP is 30 frames at 30 fps, so regular media segments are 1.000s of video (except possibly the last).
- The 400k and 900k ladders must have the *same number* of media segments and the same segment start timestamps (keyframe alignment). If a rendition would drift, it is a packager bug — the sources were encoded with identical GOP cadence.
- Audio is in its own CMAF track (separate media playlist). Audio segment times must cover the same program timeline; priming/encoder delay must not shift A/V by more than 2 audio frames (~42ms at 48 kHz) relative to the source.
- The mid-roll `ad400.mp4` is inserted starting at the cue time in `fixtures/scte35.json` (2.000s into the programme). Programme video/audio pause for the ad duration (2.000s) and resume; playlists must use `EXT-X-DISCONTINUITY` around the ad, and an `EXT-X-DATERANGE` (or `EXT-X-CUE-OUT`/`EXT-X-CUE-IN`) with the SCTE-35 cue id and planned duration.

## CENC

- Scheme `cenc` (AES-128-CTR).
- Keys and KIDs come from `fixtures/keys.json` (`program` and `ad` may use different KIDs).
- Video uses subsample encryption: NAL start codes / length prefixes and NAL headers stay in the clear; slice payloads are encrypted. Audio uses full-sample encryption.
- Init segments contain `tenc` inside a `sinf` of `encv`/`enca`. Media segments contain `senc` (and `saiz`/`saio` if you also write those) with IVs; IVs must be unique per sample per track.
- `EXT-X-KEY:METHOD=SAMPLE-AES` or `METHOD=SAMPLE-AES-CTR` plus a local `key.bin` / `key://` URI that `verify` can read. A `key.bin` per KID in the output dir is fine.

## Playlists

- `master.m3u8` lists both video renditions with `BANDWIDTH`, `RESOLUTION`, `CODECS`, and the audio group.
- Each media playlist is `#EXT-X-VERSION:7` (or higher), `#EXT-X-INDEPENDENT-SEGMENTS`, `#EXT-X-MAP`, and has `#EXT-X-ENDLIST`.
- Target duration is 1 second for this fixture (ceil of max segment duration).

## verify

`cencpack verify` must:

1. Parse the playlists and check alignment / discontinuity / daterange.
2. Decrypt the 400k programme+ad+programme video (and audio) with the test keys, concat, and compare:
   - programme frame 0..59 (2.0s) match `v400.mp4` frames 0..59 (pixel-close, not a black slate)
   - the next 2.0s are the ad (red slate)
   - the following 4.0s match `v400.mp4` frames 60..179
3. Fail if the stream is not actually encrypted (e.g. if `senc` is missing or ciphertext equals plaintext).
4. Write `/path/verify.json` with pass/fail per check.

Do not edit the fixture media.

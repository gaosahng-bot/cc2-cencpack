# cencpack

CMAF / HLS packager with CENC sample encryption, keyframe-aligned ABR and SCTE-35 ad markers.

Already in the tree:

- `cencpack.boxes` — ISO-BMFF box tree reader (ftyp/moov/trak/stbl sample tables, etc.)
- `cencpack.probe` — pretty-print a file's tracks and sync samples
- Fixtures: two H.264 ladder rungs (`v400.mp4`, `v900.mp4`), an AAC track (`audio.m4a`), a 2s mid-roll slate (`ad400.mp4`), a test key file, and a SCTE-35 cue list

Implemented modules:

- `cencpack.source` — unfragmented MP4 reader (sample tables, edit lists, avcC/esds)
- `cencpack.fragment` — keyframe-aligned CMAF init segments and `moof`/`traf`/`senc` fragments
- `cencpack.cenc` — AES-128-CTR (`cenc`) subsample video / whole-sample audio encryption, `tenc`/`sinf`
- `cencpack.align` — IDR segmentation, programme/ad splice planning, ABR alignment assertions
- `cencpack.hls` / `cencpack.playlist` — HLS master/media writers and verifier-side parser
  (`EXT-X-MAP`, `SAMPLE-AES-CTR`, `EXT-X-DISCONTINUITY`, `EXT-X-DATERANGE`)
- `cencpack.decrypt` / `cencpack.verify` — in-tree CENC decryption, MP4 rebuild and pixel-level verification

Package a fixture set, then verify the result:

```
python -m cencpack package \
  --video fixtures/v400.mp4 --video fixtures/v900.mp4 \
  --audio fixtures/audio.m4a \
  --ad fixtures/ad400.mp4 \
  --cues fixtures/scte35.json \
  --keys fixtures/keys.json \
  --out out/

python -m cencpack verify \
  --out out/ \
  --keys fixtures/keys.json \
  --source-video fixtures/v400.mp4
```

`verify` writes `out/verify.json` with a pass/fail entry per check, including
playlist structure, ABR alignment, SCTE-35 discontinuity/daterange markers,
`senc` presence, decryption of the 400k track and red-slate/programme pixel
comparisons via the system ffmpeg.

System ffmpeg/ffprobe are available and may be used as tools. Do not pull in Docker or bind against libav*. Stay on in-tree modules for packing/encryption logic.

# cencpack

CMAF / HLS packager with CENC sample encryption, keyframe-aligned ABR and SCTE-35 ad markers.

Already in the tree:

- `cencpack.boxes` — ISO-BMFF box tree reader (ftyp/moov/trak/stbl sample tables, etc.)
- `cencpack.probe` — pretty-print a file's tracks and sync samples
- Fixtures: two H.264 ladder rungs (`v400.mp4`, `v900.mp4`), an AAC track (`audio.m4a`), a 2s mid-roll slate (`ad400.mp4`), a test key file, and a SCTE-35 cue list

Not implemented:

- Keyframe-aligned CMAF fragmenter (`cencpack.fragment`)
- `cenc` sample encryption with `tenc`/`sinf`/`encv`/`enca` (`cencpack.cenc`)
- HLS master + media playlists, `EXT-X-MAP`, `EXT-X-KEY`, `EXT-X-DATERANGE` for SCTE-35 (`cencpack.hls`)
- Multi-bitrate alignment and audio priming (`cencpack.align`)
- `python -m cencpack package` and `python -m cencpack verify`

```
python -m cencpack package   --video fixtures/v400.mp4 --video fixtures/v900.mp4   --audio fixtures/audio.m4a   --ad fixtures/ad400.mp4   --cues fixtures/scte35.json   --keys fixtures/keys.json   --out /tmp/cencpack-out

python -m cencpack verify --out /tmp/cencpack-out --keys fixtures/keys.json --source-video fixtures/v400.mp4
```

System ffmpeg/ffprobe are available and may be used as tools. Do not pull in Docker or bind against libav*. Stay on in-tree modules for packing/encryption logic.

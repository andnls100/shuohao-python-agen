# shuohao-python-agen

Python agent for the `shuohao-skills` AI short-drama production pipeline.

Pipeline:

`novel → outline → characters → art → script → staging → storyboard → optional images / frames / refs / video / audio`

## Video generation: current design

The video stage generates **one storyboard cut at a time**. It does not use the old static storyboard frame as the video start frame.

For every cut, the program first decides whether it is the immediate continuation of the previous cut.

- **First cut / new scene / hard cut**
  - Fixed scene: `scene image + character refs + prop refs + prompt`
  - Non-fixed scene: `character refs + prop refs + prompt`
- **Continuous cut**
  - Requires the same episode and `sceneIndex`, `continuity.mode == "inherit"`, a `referenceFrame` pointing to the immediately previous cut, and `continuityMode == "continuous"` when crossing segments.
  - Input: `previous video's real last frame + character refs + prop refs + prompt`
- `hard_cut`, `cut`, scene changes, missing references, or an incorrect `referenceFrame` never inherit the previous video frame.

The same `sceneIndex` alone is **not** enough to declare continuity.

After a video is downloaded, FFmpeg extracts its real last frame to `output/video/last-frames/`. That image becomes the next continuous cut's first-keyframe reference.

## Most important commands

Preview one cut; no video API call:

```bash
python run_agent.py run --stages video --eps 1 --shots E01-01-C01
```

Submit the first cut:

```bash
python run_agent.py run --stages video --eps 1 --shots E01-01-C01 --video-submit
```

Submit the next cut; if storyboard marks it continuous, the previous cut must already have a successful video and extracted last frame:

```bash
python run_agent.py run --stages video --eps 1 --shots E01-01-C02 --video-submit
```

Generate an entire episode sequentially:

```bash
python run_agent.py run --stages video --eps 1 --video-submit
```

Generate selected cuts sequentially:

```bash
python run_agent.py run --stages video --eps 1 --shots E01-01-C01,E01-01-C02 --video-submit
```

Consecutive continuous cuts inside one segment are merged into a single video call (total <= 10 s by default; merged IDs look like `E01-01-C01-C02`). Tune with `--video-merge-seconds N` (0 = one call per cut).

Generate whole segments (all cuts, in order):

```bash
python run_agent.py run --stages video --eps 1 --segments E01-01,E01-02 --video-submit
```

Preview Seedance-style prompts:

```bash
python run_agent.py run --stages video --eps 1 --video-protocol seedance
```

`h3` is the default protocol and uses the storyboard `h3Prompt`.

The old `--video-protocol i2v`, `--frames-dir`, and `--video-flf` options are removed. Static `frames/` generation remains an independent optional stage; it is not the continuity source for video generation.

## Video manifest

`output/video/video-manifest.json` records the decision for every cut:

- `input_mode`: `scene_or_assets` or `previous_last_frame`
- `continuity_decision`: why the decision was made
- `previous_shot_id`: required predecessor for a continuous cut
- `scene_fixed`: whether a usable scene reference image exists
- `initial_reference`: `scene_reference`, `previous_last_frame`, or `none`
- `last_frame_path`: the extracted real last frame of the generated video

Outputs:

```text
output/video/
├── video-manifest.json
├── prompts/
├── videos/
├── last-frames/
└── qc-checklist.md
```

## Video model configuration

Configure video providers in the root `models.json` under `video`. The selected provider must support image/reference-video input for the new continuity design. The most complete current adapter is Wan R2V.

Example:

```json
{
  "video": [
    {
      "key": "wan-r2v",
      "name": "Wan R2V",
      "adapter": "aliyun_wan",
      "mode": "r2v",
      "submit_url": "https://your-provider.example/v1/videos/generations",
      "status_url_template": "https://your-provider.example/v1/tasks/{task_id}",
      "api_key_env": "VIDEO_API_KEY",
      "model": "your-r2v-model"
    }
  ]
}
```

Use the provider's official endpoint, model name, request fields and authentication. The repository does not assume that an arbitrary OpenAI-compatible endpoint supports video generation.

FFmpeg is required and must be available as `ffmpeg` on `PATH`.

## Setup

```bash
git clone https://github.com/andnls100/shuohao-python-agen.git
cd shuohao-python-agen
pip install -r requirements.txt
node --version   # Node 18+
copy models.example.json models.json   # Windows
```

The Python agent reads model endpoints from `models.json`; the real file is git-ignored.

## Common stages

```bash
python run_agent.py run --stages outline --book input/novel.txt --episodes 6 --minutes 2 --genre urban
python run_agent.py run --stages characters,art
python run_agent.py run --stages script,staging,storyboard --eps 1-2
python run_agent.py run --stages refs --refs-names Alice --refs-gen
python run_agent.py run --stages frames --frames-gen --frames-only E01-01-C01
python run_agent.py run --stages audio --eps 1 --audio-submit --yes
```

See [docs/video-generation.md](docs/video-generation.md) for the complete video workflow and `models.json` configuration details.

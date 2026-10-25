---
name: h3-prompt-writing
version: 1.0.0
source: MiniMax-AI/MiniMax-H3/skills/h3-prompt-writing/references/ref-en.txt
mode: Ref2VA only
---

# MiniMax H3 Ref2VA Prompt Writing (ComfyUI)

Use only MiniMax H3 Full-Reference / Ref2VA. Do not switch to T2VA, I2VA, FL2VA, or L2VA.

## Required output structure

Write all six sections in this exact order:

1. `subject_definitions`
2. `summary`
3. `retention_analysis`
4. `detailed_description`
5. `overall_soundscape`
6. `non_diegetic_music`

Use English for all prompt text except original dialogue/lyrics inside `<d>...</d>` and text visibly present in the scene.

## Reference semantics

- `<Subject N>`: reusable visible content such as a character, animal, prop, environment, style, pose, or action.
- `<Picture N>`: a reference image used as a concrete frame, keyframe, or composition anchor.
- `<Video N>`: a reference video used as an editing source or temporal structure.
- `<Audio N>`: a referenced audio signal.
- Keep each label's meaning and number consistent throughout all six sections. Never invent or renumber labels.
- Define each referenced subject and explain which source assets establish its appearance or role. Use a standalone `<Picture N>` definition when the image itself is used as a frame/keyframe/composition anchor.
- In `retention_analysis`, state where each referenced item is used and whether it is preserved, partially preserved, transferred, or reused.

## Detailed description

- Describe shots in playback order, including composition, subject position and appearance, environment, lighting, visible actions, state changes, camera movement, sound, and when references take effect.
- Do not reduce the description to a plot summary or a list of reference relationships.
- Use the provided storyboard cuts and timing as authoritative. Do not add or reorder events, shots, characters, props, dialogue, or sound.
- Preserve every `<d>...</d>` dialogue block character-for-character. Keep the original dialogue language inside the block.
- Separate diegetic sound/ambience from non-diegetic music. Do not invent a music track when none is specified.
- Keep the final prompt under 7,000 characters.

## Output

Return only the final prompt, with no explanation, Markdown fences, or extra heading outside the six required sections.

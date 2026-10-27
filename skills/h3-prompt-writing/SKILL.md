---
name: h3-prompt-writing
version: 1.1.0
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

### Reference tags in this pipeline (overrides the `<Picture N>` bullet above)

This pipeline sends MiniMax H3 Ref2VA one reference image per character, per scene and per prop of the segment (the design sheets). Storyboard frames are not sent. So `<Picture N>` means "reference image N of this segment" and is never a shot number.

- While writing, put the card id inside the tag: `<Picture C02>` for a character, `<Picture S01>` for the scene, `<Picture P03>` for a prop. Copy the id exactly from the cards. Never write a digit tag such as `<Picture 1>`: the program binds the assets first, then replaces each id with the final image number.
- Define every character, the scene and every prop that appears in the segment once in `subject_definitions`, each with its own tag. One tag never stands for two subjects.
- Reuse the same tags in `retention_analysis` and `detailed_description` wherever that subject, the environment or the prop is used. Only use ids that belong to this segment.
- Because the images are design sheets, describe what each one establishes (appearance, costume, environment layout, prop shape). Do not call any of them a keyframe, a first frame or a composition anchor.
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

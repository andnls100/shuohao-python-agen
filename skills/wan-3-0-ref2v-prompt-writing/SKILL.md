---
name: wan-3-0-ref2v-prompt-writing
version: 1.0.0
source: Comfy-Org/embedded-docs Wan 3.0 Reference-to-Video documentation
---

# Wan 3.0 ComfyUI Reference-to-Video Prompt Writing

This skill is only for Wan 3.0 Reference-to-Video (Ref2V) workflows in ComfyUI. Do not write T2V or ordinary single-image I2V prompts.

## ComfyUI reference syntax

- Refer to a connected image input with the exact token `@Image1`, `@Image2`, etc.
- Image numbers refer to the order of image inputs actually supplied to the ComfyUI Wan 3.0 Reference-to-Video node. Never invent an image index or assume an unconnected image exists.
- When adapting source storyboard labels such as `<Picture N>`, convert them to `@ImageN` only when the project has established that the storyboard numbering matches the ComfyUI input order. Preserve the same numeric index.
- Do not use MiniMax-specific `<Subject N>`, `<Picture N>`, `<Video N>`, or `<Audio N>` syntax in the final Wan prompt.

## Write the final prompt

Write one coherent, concrete instruction for the target clip:
1. State the intended subject and scene using the supplied reference images as identity, appearance, prop, or environment anchors.
2. Describe visible actions in chronological order, including start state, movement, interaction, reaction, and end state.
3. Specify camera framing and movement only when it contributes to the shot; camera motion must agree with the subject action.
4. Describe relevant lighting, spatial relations, and environmental changes.
5. Include dialogue, ambience, and sound effects only when supplied by the storyboard. Do not invent speech, music, characters, props, or events.
6. Keep identities, clothing, props, and spatial continuity consistent with the reference images and preceding-shot continuity notes.

## Constraints

- All prompts must be suitable for the connected Wan 3.0 Reference-to-Video ComfyUI workflow.
- Prefer concrete observable actions over abstract mood words or lists of cinematic adjectives.
- Do not write a generic negative-prompt list unless the workflow explicitly expects one.
- Do not add Markdown headings, explanations, or wrapper text to the final prompt.
- Preserve any dialogue block `<d>...</d>` exactly if one is present in the source data.
- Output only the final prompt text.

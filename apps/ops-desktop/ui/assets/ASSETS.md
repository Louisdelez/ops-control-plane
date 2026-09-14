# Visual assets

## Illustration

File: `ops-connected-arches.png`

Created with the built-in **imagegen** tool, then copied into this project without changing the generated image. No provider API key was needed.

Final prompt:

> Use case: stylized-concept. Asset type: original small illustration for the sidebar of the Ops desktop application, a modern minimal light UI with deep forest green and pale sage accents. Create a refined miniature still-life of three interlocking translucent green glass and matte ivory architectural arches, evoking connected tools and calm organization. Clean premium editorial 3D illustration, restrained soft studio lighting, gentle ambient shadow, balanced centered composition with ample negative space. Square 1024x1024 image, uniform very pale off-white background #f6f8f7. No text, no letters, no watermark, no interface mockup, no branded logos, no busy decorative elements. Designed to remain legible as a tasteful small 160px illustration.

## Icons

Lucide Static **0.468.0**, downloaded from `https://unpkg.com/lucide-static@0.468.0/icons/`. Original SVG files are kept in `icons/`, alongside the distributed license. These are semantic interface icons, not claimed to be the official logos of service providers.

Official license documentation: https://lucide.dev/license

The Ops launcher icon adapts Lucide's `layers.svg` with a green background. Its editable source is `../../src-tauri/icons/ops-layers.svg`.

## Infrastructure illustration — 0.8.2, 14 September 2026

File: `infrastructure-studio-v1.png`. Generated with the built-in **imagegen**
tool and copied unchanged into the application. Decorative illustration only;
it does not represent the actual topology or number of supervised machines.
Original preserved in the Codex generated-images directory. No CLI fallback or
provider API key was used. The image is bundled locally and needs no remote URL.

Final prompt:

> Use case: stylized-concept. Asset type: decorative hero artwork for the Infrastructure area of a premium native desktop operations dashboard. Create a polished original 3D editorial illustration of a small connected computing infrastructure: three sculptural graphite and frosted-glass server modules, one elegant storage block, thin precise teal connections and a few tiny turquoise status lights. Abstract enough to be decorative, recognizable hardware silhouette. Composition: wide landscape 2:1, the carefully arranged objects concentrated in the right two thirds, quiet empty deep midnight navy space on the left for application copy added in code. Camera: restrained isometric perspective, soft studio lighting and ambient shadows. Palette matches existing application: midnight navy #0b1018, slate #172232, pale silver, muted teal #55ddd1. Materials: tactile matte graphite, satin aluminium, subtle translucent glass. Crisp, sophisticated, understated, plenty of breathing room. No text, no letters, no numbers, no logos, no UI screenshot, no charts, no labels, no gradients with rainbow colors, no excessive neon, no cyberpunk city, no watermark. This is a real raster illustration to be embedded in an application header, not a mockup of the application.

Infrastructure icons extend the same Lucide Static **0.468.0** set already used
by Ops. Pinned original SVGs are retained in `icons/` with the existing license.
They are rendered as local CSS masks to inherit the accessible light/dark palette.

### Light counterpart

File: `infrastructure-studio-light-v1.png`, generated with the built-in imagegen
edit tool using the dark illustration as the edit target. Copied unchanged into
the project; selected automatically by the light theme. Original preserved.

Final edit prompt:

> Use case: lighting-weather. Edit target: the attached dark infrastructure illustration. Create the matching LIGHT THEME version for the same native desktop dashboard header. Preserve the exact wide 2:1 composition, camera, positions, three server towers, storage block, connections and quiet empty left side. Change only the scene lighting and background: soft bright studio daylight, a clean warm off-white #f3f6f6 floor and background, pale subtle ambient shadows, satin silver and graphite hardware with restrained mint-teal connections. Keep the servers crisp and easy to distinguish against the pale background, no washed-out grey veil. Keep the generous blank left third completely uncluttered. No text, labels, letters, numbers, logo, UI, chart or watermark. A premium understated light counterpart of the original, not a new layout.

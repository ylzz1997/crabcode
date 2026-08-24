# Crab Desktop themes and skins

Crab Desktop separates the color mode from the selected preset:

- `theme_mode`: `system`, `light`, or `dark`.
- `active_theme_id`: one built-in or user preset.
- `custom_theme_presets`: validated imported or locally edited presets. Built-ins stay in the application bundle.

Settings schema v4 migrates customized v1–v3 light/dark profiles into a local preset named `迁移的外观`. Missing, corrupt, or version-incompatible active presets fall back to `builtin.crab`.

## `.crabtheme.json`

A data-only JSON document. It contains metadata plus complete light and dark profiles. Scripts, CSS, network URLs, unknown fields, and image data are rejected.

```json
{
  "schema": "io.crabcode.theme/v1",
  "theme": {
    "id": "com.example.ocean",
    "name": "Ocean",
    "author": "Example",
    "version": "1.0.0",
    "description": "A dual-mode ocean preset.",
    "minimum_app_version": "0.1.4",
    "light": {
      "accent_color": "#087f8c",
      "background_color": "#eef7f7",
      "foreground_color": "#12363b",
      "ui_font_family": "system",
      "code_font_family": "system-mono",
      "translucent_sidebar": false,
      "contrast": 50,
      "radius_scale": 1,
      "shadow_strength": 50,
      "token_overrides": {}
    },
    "dark": {
      "accent_color": "#45d4df",
      "background_color": "#07191d",
      "foreground_color": "#e6f7f7",
      "ui_font_family": "system",
      "code_font_family": "system-mono",
      "translucent_sidebar": false,
      "contrast": 50,
      "radius_scale": 1,
      "shadow_strength": 50,
      "token_overrides": {}
    }
  }
}
```

## `.crabskin`

A ZIP archive with `manifest.json`, optional `preview/` images, and declared `assets/`. The manifest uses `io.crabcode.skin/v1`; image slots reference relative archive paths instead of data URLs.

Supported slots:

- `app_background`
- `workspace_background`
- `sidebar_overlay`
- `welcome_character_left`
- `welcome_character_right`
- `composer_frame`
- `top_trim`
- `bottom_trim`

Only PNG, JPEG, WebP, and GIF data is accepted. Imports enforce path traversal, duplicate-file, file-count, per-file, total-unpacked-size, unknown-field, unreferenced-file, and minimum-app-version checks. Skin packs cannot execute CSS or JavaScript.

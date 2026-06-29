# KiCad

This KiCad project was imported from EasyEDA and is maintained with KiCad v9.0.7.

EasyEDA 3D model files are not versioned here. They should be regenerated locally with `easyeda2kicad` and the sync helper.

## Restore 3D Models

Requirements:

- `easyeda2kicad` installed and available in `PATH`
- local central library in `~/Documents/KiCad/easyeda2kicad`

From this directory:

```bash
EASYEDA_LIBRARY="$HOME/Documents/KiCad/easyeda2kicad"

./sync_easyeda_models.py . \
  --library "$EASYEDA_LIBRARY" \
  --pull-footprint-models
```

The script copies only the required models into `EASYEDA_MODELS/`, applies known 3D alignment fixes, and fetches missing parts through `easyeda2kicad` when possible.

## After 3D Alignment Fixes

If a model is manually realigned in KiCad, push the correction back to the local library:

```bash
EASYEDA_LIBRARY="$HOME/Documents/KiCad/easyeda2kicad"

./sync_easyeda_models.py . \
  --library "$EASYEDA_LIBRARY" \
  --push-to-library
```

Other projects can then reuse those fixes with `--pull-footprint-models`.

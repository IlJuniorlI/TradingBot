# Preset Config Notes

This directory contains shipped strategy presets such as:

- `config.peer_confirmed_key_levels.yaml`
- `config.peer_confirmed_key_levels_1m.yaml`
- `config.peer_confirmed_trend_continuation.yaml`
- `config.peer_confirmed_htf_pivots.yaml`
- the other `config.<strategy>.yaml` files for the rest of the package

General rules:

- `config.example.yaml` is the canonical full-config template for a custom install and for scaffolded presets.
- `config.yaml` is the default file used by `main.py` when no explicit config path is supplied.
- Shipped full presets include an explicit `strategies.<name>` block so each preset can run as a portable standalone file.
- At runtime, the selected top-level config file is the single source of truth above manifest/code defaults.

When in doubt:

1. Use `config.example.yaml` for a fresh setup or as the canonical template when maintaining top-level config structure.
2. Use `config.<strategy>.yaml` when you want a shipped tuned preset for a specific strategy.
3. Check `configs/config.<strategy>.yaml` for the shipped runtime preset.

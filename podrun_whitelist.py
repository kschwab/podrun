# Vulture whitelist for podrun.py — suppress false positives.
# These symbols are used at runtime or in downstream phases.

_extract_label_value  # unused function (used in _handle_run)
_expand_export_tilde  # unused function (used in build_overlay_run_command)
_.required  # argparse subparsers attribute
_._devcontainer  # dynamic attribute on ParseResult
_._podrun_cfg  # dynamic attribute on ParseResult

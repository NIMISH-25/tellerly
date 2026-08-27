# Root conftest so pytest puts the repo root on sys.path, making the
# non-installed `target_app` package importable from tests. `target_app` is the
# proxy target the automation drives — deliberately not part of the installed
# `tellerly` distribution.

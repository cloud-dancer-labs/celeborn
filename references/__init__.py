# Marks references/ as the `celeborn_refs` data package (mapped in pyproject.toml) so that
# schema.sql and templates/ ship inside wheels / uv-tool installs. No runtime code lives here;
# celeborn.py resolves this directory via importlib.resources.files("celeborn_refs").

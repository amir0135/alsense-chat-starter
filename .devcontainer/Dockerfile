# Development container: Python 3.12 and Git from the pinned Dev Containers image, plus the uv version that the
# project, CI and the app image use. Update the tags and digests together.
FROM mcr.microsoft.com/devcontainers/python:3.2.6-3.12-bookworm@sha256:589b65db720b03c96d721706005f176ff467ee879f92c2d137c04a537feffa56

COPY --from=ghcr.io/astral-sh/uv:0.9.11@sha256:5aa820129de0a600924f166aec9cb51613b15b68f1dcd2a02f31a500d2ede568 /uv /uvx /usr/local/bin/

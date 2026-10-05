# Installing with pipx

[pipx](https://pipx.pypa.io/) is a tool for installing Python CLI applications in isolated environments. It does not require [uv](https://docs.astral.sh/uv/).

## Install Specify CLI

Install the fork's default branch, or pin a tag from [Tags](https://github.com/Axiweave/spec-kit/tags) when one exists:

```bash
# Install the fork's default branch
pipx install git+https://github.com/Axiweave/spec-kit.git

# Or pin a tag (replace vX.Y.Z, keeping the leading v)
pipx install git+https://github.com/Axiweave/spec-kit.git@vX.Y.Z
```

## Verify

```bash
specify version
```

## Upgrade

```bash
pipx install --force git+https://github.com/Axiweave/spec-kit.git
```

## Uninstall

```bash
pipx uninstall specify-cli
```

## Next steps

Head to the [Quick Start](../quickstart.md) to initialize your first project.

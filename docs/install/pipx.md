# Installing with pipx

[pipx](https://pipx.pypa.io/) is a tool for installing Python CLI applications in isolated environments. It does not require [uv](https://docs.astral.sh/uv/).

## Install Specify CLI

Install the fork's default branch, or pin a fork commit SHA. The fork publishes no release tags. Tags such as `v1.1.0` mirror upstream and hold upstream code.

```bash
# Install the fork's default branch
pipx install git+https://github.com/Axiweave/spec-kit.git

# Or pin a fork commit (replace <sha> with a commit SHA)
pipx install git+https://github.com/Axiweave/spec-kit.git@<sha>
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

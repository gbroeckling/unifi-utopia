# Contributing to UniFi Utopia

Thanks for your interest in contributing to UniFi Utopia! This is a Home Assistant
custom integration for UniFi network device review and quarantine.

## Reporting Bugs

Please use the [Bug Report](https://github.com/gbroeckling/unifi-utopia/issues/new?template=bug_report.yml)
issue template. Include your UniFi Utopia version, Home Assistant version, and
controller model so we can reproduce the problem.

## Requesting Features

Use the [Feature Request](https://github.com/gbroeckling/unifi-utopia/issues/new?template=feature_request.yml)
template. Describe your use case so we understand *why* the feature matters.

## Development Setup

1. Fork the repository and clone it.
2. Copy (or symlink) the `custom_components/unifi_utopia` folder into your Home
   Assistant `config/custom_components/` directory.
3. Restart Home Assistant to load the integration.
4. Alternatively, add this repo as a HACS custom repository for easier testing:
   - HACS > Integrations > three-dot menu > Custom repositories
   - URL: `https://github.com/gbroeckling/unifi-utopia`
   - Category: Integration

## Code Style

- **Python** — Follow standard Home Assistant conventions (`black`, `isort`,
  type hints where practical).

## Pull Request Process

1. Create a feature branch from `main`.
2. Keep commits focused — one logical change per commit.
3. Test your changes against a running Home Assistant instance.
4. Open a PR to `main` with a short description of what changed and why.
5. A maintainer will review and merge when ready.

## How This Project Is Built

UniFi Utopia is built by [Garry Broeckling](https://github.com/gbroeckling).
Architecture, product decisions, testing, and releases are human-directed.
Implementation is AI-assisted using [Claude](https://claude.ai) by Anthropic.

If you contribute a PR, there is no requirement to use (or not use) AI tools.
Write code however you are most productive — what matters is that it works,
follows the existing patterns, and passes review.

## Code of Conduct

Be kind, be constructive. We are all here to make network security better.

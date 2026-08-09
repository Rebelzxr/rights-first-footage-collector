# Contributing

Thanks for helping make footage collection safer and easier to audit.

## Before opening an issue

- Search existing issues first.
- Remove API keys, signed download URLs and personal file paths.
- Do not attach downloaded stock footage or cached provider responses.
- Include the collector version, operating system, Python version and the redacted command shape.

## Proposing a provider

A new provider must have:

- An official API.
- Clear commercial-use terms.
- Stable creator, source-page and provider-ID fields.
- A documented rate-limit policy.
- HTTPS media delivery.
- Tests that prove secrets and signed URLs do not enter public manifests.

Scraping social platforms or search-result pages will not be accepted.

## Pull requests

1. Fork the repository and create a focused branch.
2. Keep changes small and explain the safety boundary they affect.
3. Add or update tests for every behaviour change.
4. Run:

   ```bash
   python3 -m unittest discover -s tests -v
   ```

5. Confirm the diff contains no secrets, media files, provider caches or personal paths.
6. Open a pull request using the supplied template.

By contributing, you agree that your contribution is licensed under the MIT License.

## Review standard

Maintainers will check:

- Secret and log safety.
- Provider-policy compliance.
- Rights-receipt completeness.
- Download and filesystem boundaries.
- Duplicate-detection behaviour.
- Backward compatibility of manifest fields.
- Test coverage for failure paths.

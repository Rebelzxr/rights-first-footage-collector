## Change

Describe the smallest user-visible change.

## Safety boundary

Explain any effect on secrets, provider policy, downloads, filesystem paths, manifests or duplicate detection.

## Verification

- [ ] `python3 -m unittest discover -s tests -v` passes.
- [ ] No API keys, signed URLs, provider caches, downloaded media or personal paths are included.
- [ ] New behaviour has tests.
- [ ] Documentation and manifest schema are updated when needed.
- [ ] Provider changes use an official API and link current terms.

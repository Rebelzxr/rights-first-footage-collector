# Security policy

## Supported version

Security fixes are applied to the latest release on the default branch.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability involving API-key exposure, unsafe redirects, path traversal, cache disclosure or arbitrary file writes.

Use GitHub's private vulnerability-reporting feature under **Security → Advisories → Report a vulnerability**. Include a minimal reproduction with all credentials and signed URLs replaced by placeholders.

Do not include real provider responses, downloaded media, private catalogs or `.env` files.

## Security boundaries

- This project is not a licence-verification authority or legal-advice service.
- Provider responses and media metadata are untrusted input.
- The private API cache may contain temporary provider download URLs.
- Human review of source pages and actual frames remains mandatory.
- Users control the output and catalog paths supplied on the command line.

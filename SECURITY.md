# Security

Please report vulnerabilities privately through GitHub's [private vulnerability reporting](https://github.com/marshal-ledger/marshal/security/advisories/new) rather than in a public issue.

Marshal handles agent definitions, which can contain sensitive configuration. By design:

- readers copy fields from allowlists, never copy credentials, and record environment variables by name only;
- free text is passed through credential redaction before it is stored;
- hash-only mode stores instruction hashes instead of text;
- Marshal never connects to agent platforms. Pipelines push to it, authenticated with short-lived OIDC tokens.

See [Security posture](docs/design.md#security-posture) and [Network posture](docs/design.md#network-posture).

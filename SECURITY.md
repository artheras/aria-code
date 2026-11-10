# Security Policy

Thank you for helping keep Aria Code and its users secure.

## Supported Versions

Security fixes target the latest `main` branch and the latest tagged release.
Older development branches are not maintained after their changes are merged or
closed. If reporting an older version, please note whether the issue also affects
a supported version.

## Reporting a Vulnerability

Please do not disclose suspected vulnerabilities, exploit details, credentials,
or sensitive reproduction data in public issues or pull requests.

Report privately through **Security → Report a vulnerability** on GitHub, if
available for this repository, or email **security@arthera.xyz**. Use public
GitHub issues for ordinary bugs, documentation, and feature requests that do
not disclose a suspected vulnerability.

To help us investigate, include:

- the affected component, version or commit, operating system, and installation method;
- relevant configuration, permission settings, and integrations;
- the attacker's starting access and the security boundary or control affected;
- minimal reproduction steps, expected and actual behavior, and potential impact;
- sanitized logs or screenshots when helpful.

Remove API keys, access tokens, customer data, and unrelated private information
from reports. Do not include live credentials in a proof of concept. If you find
a vulnerability in a third-party service or repository, follow its security
policy and report it to its maintainers.

## Operating Aria Code Safely

Aria Code may access files, run commands, call external tools, and process data
under the permissions available in its environment. Review permission settings,
proposed commands, file changes, and external actions before approving them.
The protection available depends on the chosen configuration and integrations;
do not assume a prompt, a model response, or a third-party tool is a security
boundary.

Treat repository content, documents, tool results, and MCP servers as potentially
untrusted. Connect only services you trust and are authorized to use, grant the
minimum access needed, and avoid placing secrets in prompts or logs. Follow your
organization's rules when working with customer or financial data.

## Credential Handling

Never commit real API keys, broker credentials, tokens, `.env` files, private
certificates, or production config files. Commit only templates such as
`.env.example`, `.env.daemon.template`, and `config/*.example.*`.

## Broker Safety

Live broker execution must remain opt-in. Real broker adapters should default to
read-only, require explicit preview/confirmation boundaries, and write audit
events for previews, rejections, and executions.

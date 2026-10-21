# mailsocket

mailsocket is an inbox API for OTP and magic-link testing, signup automation,
and AI agents. Email in, JSON out.

Features: disposable inboxes, extracted verification codes and magic links,
long-poll wait, webhooks, and API keys.

Architecture: Django serves the API and dashboard. PostgreSQL stores inboxes
and messages. Postfix accepts catch-all inbound email. An ingest endpoint stores
the raw message before parsing it. Docker Compose runs the services.

## Self-host

Install Docker with Compose. Provision a domain/subdomain and point its MX
record to your mail server (port 25 must accept inbound SMTP). Copy
`.env.example` to `.env` and replace placeholder secrets, domain, hosts, and
tunnel settings; provision the referenced secret files and DNS records. Run
`docker compose up --build -d`, then
`docker compose exec web python manage.py createsuperuser`. In the dashboard,
create an API key. See the deployment configuration for the Postfix database
reader and signing-key requirements. A Cloudflare Tunnel token is needed for
the bundled tunnel service.

Prefer hosted? The free service is at https://mailsocket.app.

SDKs: [Python](sdks/python), [TypeScript](sdks/typescript), [MCP](sdks/mcp),
[n8n](sdks/n8n). Examples: [Playwright](examples/playwright).

Licensed under AGPL-3.0; see [LICENSE](LICENSE).

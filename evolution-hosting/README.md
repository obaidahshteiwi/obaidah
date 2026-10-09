# Syria Mubasher WhatsApp bridge (Evolution API)

This folder contains a self-hosted Evolution API stack for the Syria Mubasher GitHub Actions workflow.

## Important
- This is a server deployment template, not a hosted service by itself.
- Do not commit the server's real `.env` file or share its API key.
- Use a VM with a persistent disk and a stable public IPv4. Free web hosts that sleep or erase local storage are not suitable for keeping a WhatsApp Web session alive.
- WhatsApp group/channel support can vary by Evolution API version. Test a non-critical group and channel before relying on it for production.

## Server setup (Ubuntu VM)
1. Create a small Linux VM with a persistent boot volume and a public IPv4.
2. Allow inbound TCP ports 22 (SSH), 80 and 443 in the cloud firewall/security list. Do not expose ports 5432, 6379, or 8080 publicly.
3. Install Docker Engine and the Docker Compose plugin.
4. Copy this folder to the VM.
5. Copy `.env.example` to `.env`, then set `API_DOMAIN` to `PUBLIC_IPV4.nip.io` and replace all three placeholder secrets with long random strings.
6. Start the stack with `docker compose up -d`.
7. Open `https://API_DOMAIN` to check the API is reachable. Caddy obtains HTTPS automatically when DNS resolves to the VM and ports 80/443 are reachable.
8. Create an Evolution instance through its manager/API, scan the QR code with the WhatsApp account, and test sending a message to the group and channel.

## GitHub Actions secrets
After the API and WhatsApp instance are tested, add these repository Actions secrets:
- `EVOLUTION_API_URL`: full HTTPS base URL, e.g. `https://PUBLIC_IPV4.nip.io`
- `EVOLUTION_API_KEY`: same value as `EVOLUTION_API_KEY` in the server's `.env`
- `EVOLUTION_INSTANCE`: exact instance name created in Evolution
- `WHATSAPP_GROUP_ID`: exact group JID, usually ending in `@g.us`
- `WHATSAPP_CHANNEL_ID`: exact channel/newsletter JID supported by your Evolution version

Do not remove Green-API secrets until both destinations pass real send tests.

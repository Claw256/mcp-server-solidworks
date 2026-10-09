# SolidPilot remote gateway (Vercel)

Lets Claude (web and mobile) use SolidPilot as a **custom connector**, while SolidWorks keeps running on your PC.

```
Claude ──HTTPS + OAuth──▶ gateway on Vercel ◀── outbound HTTPS long-poll ── agent.py (your PC)
                              │  Redis (Upstash): OAuth state, job queue, tool catalogue      └─▶ localhost:5000 ─▶ C# layer ─▶ SolidWorks
```

* **Gateway** (`gateway/`): the MCP server Claude connects to (`/mcp`, Streamable HTTP, stateless, JSON responses). It is its own single-owner OAuth 2.1 authorization server (dynamic client registration, PKCE S256, rotating refresh tokens, RFC 8707 audience binding) and holds no CAD logic.
* **Agent** (`adapters/claude/agent.py`): runs the unchanged tool code from `server.py` on the PC. It makes **outbound HTTPS calls only**: it pushes its tool catalogue, long-polls `/agent/poll` for jobs and posts results to `/agent/result`, authenticating with an OAuth `client_credentials` token (audience `/agent`; it cannot call tools, and user tokens cannot poll).
* **Why Redis:** Vercel functions are stateless and may run on many instances, with an ephemeral disk. Everything shared lives in Redis: OAuth clients/codes/tokens, brute-force lockouts, the job queue and the catalogue.
* **Local `stdio` use is unchanged.** `python server.py` still works for Claude Desktop.

Claude's side of the contract (callback URL, DCR, PKCE, 401 + `resource_metadata`, 240 s tool-call cap, ~150k-character results) is documented at <https://claude.com/docs/connectors/building/authentication>; the gateway follows it.

## 1. Deploy with the Vercel CLI

Run everything from the `gateway/` folder (it is the Vercel project root).

```powershell
cd gateway
vercel login
vercel link                      # create/link a project, e.g. "solidpilot-gateway"
```

**Redis.** In the Vercel dashboard: your project → *Storage* → *Create Database* → **Upstash Redis** (Marketplace) and connect it to the project (Production). It injects `REDIS_URL`/`KV_URL` (a `rediss://` TCP URL). BLPOP needs the TCP URL, not the REST one.

**Secrets and settings** (generate long random values; values are read from stdin so they stay out of your shell history):

```powershell
vercel env add GATEWAY_OWNER_SECRET production --sensitive
vercel env add AGENT_CLIENT_ID production                   # e.g. solidworks-pc
vercel env add AGENT_CLIENT_SECRET production --sensitive
vercel env add GATEWAY_PUBLIC_URL production                # the exact URL Claude will use (see below)
```

> **Windows PowerShell 5.1 gotcha:** piping a value into `vercel env add` prepends a UTF-8 BOM, which corrupts the value (the gateway now strips it, but a secret you also copy elsewhere would still differ). Add values from Git Bash (`printf '%s' "$VALUE" | vercel env add NAME production --force --yes`) or paste them when prompted.

`GATEWAY_PUBLIC_URL` must equal the production domain, e.g. `https://solidpilot-gateway.vercel.app` (or your custom domain). Deploy once to learn the domain if needed, set the variable, then redeploy:

```powershell
vercel deploy --prod
curl https://<your-domain>/health          # {"ok":true,"agent_connected":false}
```

If `/health` or `/mcp` answers with a Vercel login page, turn off **Deployment Protection** for production (Project → Settings → Deployment Protection): Claude cannot sign in to Vercel.

Hobby plan: function duration is 300 s, which covers Claude's 240 s cap. Vercel's Hobby terms are for non-commercial use; use Pro for commercial use.

## 2. Run the agent on the SolidWorks PC

```powershell
cd adapters\claude
pip install -r requirements.txt
# add the GATEWAY_URL / AGENT_* lines from .env.agent.example to adapters\claude\.env
python agent.py
```

It reconnects with backoff. Start it at logon (Task Scheduler "At log on", action `python.exe agent.py`, start in `adapters\claude`). `GET /health` shows `agent_connected` while it is polling.

## 3. Add the connector in Claude

Claude → Customize → Connectors → **Add custom connector** → URL `https://<your-domain>/mcp`. Claude registers itself, opens the gateway's sign-in page, and you enter `GATEWAY_OWNER_SECRET`. It then appears on mobile too.

The only accepted OAuth redirect URIs are `https://claude.ai/api/mcp/auth_callback` (the documented one) and the `claude.com` variant; add others with `GATEWAY_EXTRA_REDIRECT_URIS`.

## Working with files from a phone

The remote caller cannot touch the PC's disk, so the agent adds two tools:

* `stage_file(filename, content_base64)` writes an uploaded PDF/image/DXF/CAD file into the staging folder (`AGENT_STAGING_DIR`, default `~/SolidPilotStaging`, swept after `AGENT_STAGING_TTL_HOURS`) and returns the path to pass to `open_document`, `prepare_drawing`, `analyze_drawing`...
* `get_file(file_path)` returns a file from the staging folder (or `AGENT_FILE_ROOTS`) as an image or embedded resource, so exports can be sent back. Export into the staging folder first.

Both enforce an allow-list of extensions / folders and `AGENT_MAX_FILE_MB`. Results are capped at about 4 MB by the host's request-body limit (and Claude's own ~150k-character limit), so large exports will be refused with `RESULT_TOO_LARGE`. All other tools still take paths that exist **on the PC**.

## Delivering finished documents (`deliver_document`)

Files too big for a tool result (a `.sldprt`/`.sldasm`/`.slddrw` is often tens of MB) go through **private Vercel Blob** and come back as a **one-time download link**:

1. Create a **private** Blob store (Vercel dashboard -> Storage -> Blob -> Private, or `vercel blob create-store <name> --access private`) and connect it to the project. Set `BLOB_READ_WRITE_TOKEN` (the integration injects it). Optional: `GATEWAY_DELIVERY_TTL` (seconds, default 3600), `GATEWAY_DELIVERY_MAX_MB` (default 50), `CRON_SECRET` (enables the daily `/internal/sweep` cron that deletes expired blobs; blobs are also swept on every new delivery). Without `BLOB_READ_WRITE_TOKEN` the tool answers `DELIVERY_UNAVAILABLE`.
2. Claude calls `deliver_document` (no arguments = save and deliver the active document; `format` = export STEP/IGES/STL/PDF/DWG/DXF and deliver that; `file_path` = an existing file in the staging folder). The gateway binds the call to the caller's OAuth client and injects a secret slot id (hidden from the model's schema). The PC agent asks `/agent/upload-url` for a presigned PUT pinned to that exact size, content type and path, uploads **directly to Blob** (the bytes never touch a Vercel Function, so the 4.5 MB body limit does not apply), then calls `/agent/upload-complete`; the gateway checks the stored size and mints the link.
3. The link `https://<domain>/dl/<token>` is 256-bit random (only its SHA-256 is stored), valid for one hour and **works exactly once**: the first request atomically burns it and is redirected (302, `no-store`) to a 60-second presigned Blob URL; every later or unknown request gets a generic 404, and repeated misses from one IP are locked out. The blob is deleted shortly after use or at expiry.

The link is the credential, so it is only ever returned inside the chat that asked for it. Whether the Claude client lets its code sandbox reach the gateway domain is outside this server's control; opening the link in a browser always works. Blob URL signing follows `@vercel/blob`'s scheme (the Python SDK has none) and is pinned by golden vectors in `tests/test_blobstore.py`.

## Security model

* Only the owner secret grants user tokens; 5 wrong attempts lock the sign-in (shared across instances) for an increasing delay. Tokens, codes and refresh tokens are stored only as SHA-256 digests; codes and refresh tokens are redeemed atomically, so a replay fails.
* User tokens are bound to `<public>/mcp`; agent tokens to `<public>/agent`; each verifier rejects the other kind.
* Dynamic registration only accepts the allow-listed redirect URIs; `Host`/`Origin` are validated (DNS-rebinding protection).
* The PC opens no listening port. Treat `AGENT_CLIENT_SECRET` like a password: anyone holding it can impersonate the PC and see your tool calls.
* A caller that can reach the tools can do whatever the tools can (open/save/export on the PC). Keep the owner secret private.

## Behaviour when the PC is off

`tools/list` is served from the last catalogue the agent pushed, so it works with the PC off. Tool calls return `AGENT_OFFLINE: ...` within seconds (presence expires about 60 s after the agent stops polling), so the model can tell you. A call the PC never picks up is withdrawn from the queue; calls time out after `GATEWAY_CALL_TIMEOUT` (200 s).

## Local development and tests

```powershell
pip install -r gateway/requirements-dev.txt
cd gateway; python -m pytest        # OAuth flow, relay, shared-state, and the real agent against the real gateway (fakeredis, C# stubbed)
```

To run the gateway locally set the variables from `.env.example` in `gateway/.env` (with `GATEWAY_REDIS_URL` pointing at any Redis) and run `python -m solidpilot_gateway`.

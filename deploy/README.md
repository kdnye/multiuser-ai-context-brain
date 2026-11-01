# Deploying the brain (Google Cloud Run + Cloud Storage + Microsoft Entra ID)

This is the tested reference deployment. Other hosts work if they can run a container and you supply a `Store`
backend (see ARCHITECTURE "Storage"); the identity gate is Entra-only today (see docs/roadmap.md).

Placeholders used below: `<PROJECT>` (GCP project id), `<PROJECT_NUMBER>`, `<REGION>` (e.g. `us-central1`),
`<SERVICE>` (Cloud Run service name, e.g. `context-brain`), `<BUCKET>` (silo bucket), `<RUN_SA>` (runtime service
account email, `brain-run@<PROJECT>.iam.gserviceaccount.com` after step 1), `<TENANT>` (Entra tenant id),
`<API_CLIENT_ID>` (the API app registration's client id), `<DOMAIN>` (your email domain).

You need a Google Cloud project with billing, the `gcloud` CLI signed in as someone who can create the resources
below (and holds `roles/iam.serviceAccountUser` on `<RUN_SA>` to deploy as it), and Entra rights to create app
registrations and grant admin consent.

Every `gcloud` command names `--project` explicitly: a terminal's active project may be a different application.
Use additive `--update-env-vars` / `--update-secrets` only, never `--set-env-vars` (it silently drops every other
variable).

## 1. One-time infrastructure
Work out the service URL first: the Entra registration (step 2) and the server (step 4) both need it, and Cloud Run's
deterministic URL is `https://<SERVICE>-<PROJECT_NUMBER>.<REGION>.run.app`.
```bash
gcloud projects describe <PROJECT> --format='value(projectNumber)'      # <PROJECT_NUMBER>
URL=https://<SERVICE>-<PROJECT_NUMBER>.<REGION>.run.app                 # use this exact value everywhere below
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  storage.googleapis.com --project <PROJECT>
gcloud artifacts repositories create context-brain --repository-format=docker --location <REGION> --project <PROJECT>
gcloud iam service-accounts create brain-run --display-name "Context brain runtime" --project <PROJECT>
gcloud storage buckets create gs://<BUCKET> --location <REGION> --uniform-bucket-level-access \
  --public-access-prevention --project <PROJECT>
gcloud storage buckets update gs://<BUCKET> --versioning --project <PROJECT>
gcloud storage buckets add-iam-policy-binding gs://<BUCKET> \
  --member serviceAccount:<RUN_SA> --role roles/storage.objectAdmin --project <PROJECT>
```
Add a lifecycle rule that deletes noncurrent versions after 90 days, so overwritten pages stay restorable for a while
but do not accumulate forever:
```bash
cat > /tmp/lifecycle.json <<'JSON'
{"rule": [{"action": {"type": "Delete"}, "condition": {"isLive": false, "daysSinceNoncurrentTime": 90}}]}
JSON
gcloud storage buckets update gs://<BUCKET> --lifecycle-file=/tmp/lifecycle.json --project <PROJECT>
```

## 2. Entra app registrations
Follow [connectors.md](connectors.md): one API registration (exposes `brain.access`, defines the app roles, requires
assignment) plus one OAuth client registration per AI product (Claude, ChatGPT). Read the "Lessons learned" there
before you start; every item cost a failed sign-in.

## 3. Build from a reviewed commit
Build only from a merged, clean checkout. Record the commit, build id and image digest together; deploy by digest.
```bash
git status --porcelain            # must be empty
gcloud builds submit . --config=cloudbuild.yaml \
  --substitutions="_REVISION=$(git rev-parse HEAD),_REPO=context-brain,_REGION=<REGION>" \
  --project=<PROJECT> --region=<REGION>
```
Get the digest of the image you just built (deploy by digest, not by a moving tag):
```bash
gcloud artifacts docker images describe \
  <REGION>-docker.pkg.dev/<PROJECT>/context-brain/server:$(git rev-parse HEAD) \
  --format='value(image_summary.digest)' --project <PROJECT>          # sha256:... = <DIGEST>
```
The Dockerfile copies only dependencies, `mcp_server/` and `tools/`. Silos, tests, examples and local environments
never enter the image (`.gcloudignore`, `.dockerignore`).

## 4. Deploy
The server refuses to start without its public URL (`$URL` from step 1).
```bash
gcloud run deploy <SERVICE> --image <REGION>-docker.pkg.dev/<PROJECT>/context-brain/server@sha256:<DIGEST> \
  --service-account <RUN_SA> --region <REGION> --project <PROJECT> \
  --allow-unauthenticated --max-instances 1 --min-instances 0 --cpu 1 --memory 512Mi \
  --update-env-vars "MCP_TRANSPORT=http,BRAIN_STORE=gcs,BRAIN_BUCKET=<BUCKET>,MCP_PUBLIC_URL=$URL,ENTRA_TENANT_ID=<TENANT>,ENTRA_AUDIENCES=<API_CLIENT_ID>,ENTRA_SCOPES=$URL/mcp/brain.access,ALLOWED_EMAIL_DOMAINS=<DOMAIN>"
gcloud run services describe <SERVICE> --region <REGION> --project <PROJECT> \
  --format='value(metadata.annotations."run.googleapis.com/urls")'      # must list $URL
```
Cloud Run may also report an older hashed `*.a.run.app` address (`status.url`). The server accepts requests only on
`MCP_PUBLIC_URL`'s host, so give users and the Entra registration `$URL` only.

**Comma lists.** `--update-env-vars` splits on commas, so a second audience or domain breaks the command. Switch the
delimiter with gcloud's escape syntax, for example:
`--update-env-vars "^;^ENTRA_AUDIENCES=<id1>,<id2>;ALLOWED_EMAIL_DOMAINS=example.com,example.org"`.
- `--allow-unauthenticated` is deliberate: AI clients cannot present Google IAM credentials. The Entra gate in the
  server is what admits users; every path except the discovery document needs a valid token.
- `--max-instances 1` and a single serving revision are required: each process caches pages in memory and assumes it
  is the only writer.
- Optional: `BRAIN_ROLE_SILOS`, `BRAIN_SHARED_EDITOR_ROLE`, `BRAIN_NAME` (README "Configuration").
- Never set `BRAIN_DEV_EMAIL` on the hosted service; it refuses to start if you do.

## 5. Publish the shared silo (merging does not publish)
The live `shared` silo is the bucket (`silos/shared/`). The server holds a silo's pages in memory from the first
request that touches that silo until the instance restarts.
1. **Prefer a write through the server.** A shared editor calling `wiki_write` (silo `shared`) validates the page,
   rebuilds `index.md`, `log.md` and `reference/start-here`, and updates the running instance at once.
2. **First publication / bulk seed:** build the silo through the engine, then upload, then restart:
   ```bash
   python -m tools.seed --src examples/seed/shared --dest build/shared      # or your own reviewed seed folder
   BRAIN_GCS_TOKEN=$(gcloud auth print-access-token) \
     python -m tools.wiki push --wiki build/shared --bucket <BUCKET> --prefix silos/shared
   gcloud run services update <SERVICE> --update-labels last-publish=pub$(date +%Y%m%d%H%M) \
     --region <REGION> --project <PROJECT>
   ```
   A direct bucket write is invisible until the instance restarts (the label-only update above rolls a new revision
   with the same image and env). `tools.wiki push` never overwrites an existing page without `--force`; never
   `--force` a whole seed over live content.

## 6. Release checks (before you call it live)
1. `curl https://<service-url>/.well-known/oauth-protected-resource` returns the discovery document naming your
   tenant.
2. `curl -i https://<service-url>/mcp` returns 401 with a `WWW-Authenticate` header pointing at that document.
3. A real user signs in from Claude and lists the shared area (connector how-to in the seed).
4. An unassigned user is refused.
5. Two users each write a page to `me` and cannot read each other's.
6. A user without a role silo's role is refused that silo; a user with it can read and write it.

Local tests sign their own tokens; they prove the gate's logic, not your tenant's consent and OAuth settings.

## Operations
- Logs are one JSON line per request and per access decision (no tokens, emails or page content). Filter on
  `jsonPayload.allowed=false` to see refusals.
- Restore a page: list versions with `gcloud storage ls -a gs://<BUCKET>/silos/<silo>/<section>/<slug>.md`, copy the
  wanted generation back over the live object
  (`gcloud storage cp gs://<BUCKET>/silos/<silo>/<section>/<slug>.md#<GENERATION> gs://<BUCKET>/silos/<silo>/<section>/<slug>.md`),
  then restart as in step 5.
- Back up a silo to a folder: `python -m tools.wiki pull --bucket <BUCKET> --prefix silos/<silo> --dest <folder>`.
- Operators with bucket access can read every silo outside the connector; keep that group small and say so to users
  (STANDARDS 6b).

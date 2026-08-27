# IssueBot Private Portal

IssueBot is a private, multi-user Drips Wave automation portal with two separate sites. The owner manages accounts in an admin-only panel. Each normal user connects their own Drips session and optional AI API keys; the worker then fills and rotates that user's application slots automatically.

The server source can remain in a private repository. Users receive only the hosted website and the optional compiled connector. Browser-delivered HTML/CSS is inherently visible, and compiled programs can be reverse-engineered, so client-side distribution cannot provide absolute source secrecy.

## Architecture

```text
Admin panel        create, disable, reset, and delete user accounts
User portal        Drips, AI writer, fallback, priorities, and activity
PostgreSQL         users, encrypted Drips sessions, issues, and activity
Background worker scans and applies separately for every enabled user
User connector     imports a Drips session using a one-time code
```

- `dashboard.py`: separate authenticated user portal and admin panel.
- `worker.py`: per-user Drips monitoring worker.
- `wave_service.py`: fail-closed Drips API client.
- `models.py`: tenant-scoped SQLAlchemy models.
- `security.py`: Fernet credential encryption.
- `connector.py`: user-facing one-time Drips connection helper.

## Security model

- There is no public registration.
- The administrator creates every user and temporary password.
- Admin and normal-user sign-in routes and permissions are separate.
- Users must change their temporary password on first login.
- Drips sessions are encrypted at rest.
- Users can only query or modify records belonging to their own account.
- All state-changing browser forms use CSRF protection.
- Login attempts are throttled.
- The service does not collect GitHub passwords.
- Drips sessions and Gemini, DeepSeek, and OpenAI API keys are encrypted at rest.
- Saved secrets are never returned to the browser after submission.
- Each user explicitly enables or pauses automatic applications for their own account.

Each participant must use their own Drips account and KYC identity. Account sharing is prohibited by the [Drips Wave terms](https://docs.drips.network/wave/terms-and-rules/).

## Local setup

1. Install dependencies with `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and generate strong secrets.
3. Start the portal and worker with `python run.py`.
4. Open `http://localhost:5000/admin/login` and sign in with `ADMIN_USERNAME` and `ADMIN_PASSWORD`.

Generate the encryption key with:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Administrator flow

1. Sign in at `/admin/login`. Admin accounts cannot enter the normal user portal.
2. Create a username and temporary password.
3. Give those credentials to the intended user through a secure channel.
4. Disable the user at any time to stop monitoring and block access.
5. Reset a password, or permanently delete the user and all their IssueBot data.

## User flow

1. Sign in at `/login` and replace the temporary password.
2. Open **Setup**.
3. Generate a one-time Drips connection code.
4. Run the compiled IssueBot Connector, enter the website URL and code, then log in to Drips in the browser window it opens.
5. Optionally connect Gemini, DeepSeek, and OpenAI keys and choose the preferred provider. IssueBot tries the other connected providers if the preferred provider fails.
6. Set a fallback message, slot limit, two-per-repository limit, 30-minute rotation, and priority repositories.
7. Enable automatic applications and watch applications, withdrawals, assignments, and errors in the Activity feed.

The worker prioritizes configured repository owners or exact `owner/repository` names. When all slots are full, a new priority issue can replace the oldest non-priority application. Pending applications older than the configured rotation period are withdrawn. When an application is accepted, that exact repository is added to the user's priority list automatically.

If no AI key is connected, or every connected provider fails or reaches quota, IssueBot uses the user's fallback message. The default is `Hi, I can fix this`.

Connection codes expire after 15 minutes and can only be used once. The connector does not save the Drips session locally.

## Build the Windows connector

The connector contains no administrator credentials, database access, or service source. Build it on Windows:

```powershell
pip install pyinstaller playwright requests
python build_connector.py
```

Give users only `dist\issuebot-connector.exe` and your HTTPS portal URL.

## Build the Linux connector

The Linux connector is built natively on 64-bit Ubuntu and bundles Chromium,
so the recipient does not need Python or a separate browser installation.
Run the **Build Linux connector** workflow from the repository's GitHub Actions
page, then download the `issuebot-connector-linux-x86_64` artifact.

On Linux, extract and run it:

```bash
tar -xzf issuebot-connector-linux-x86_64.tar.gz
chmod +x issuebot-connector
./issuebot-connector
```

To build it manually on a Linux desktop instead:

```bash
python3 -m pip install pyinstaller playwright requests
python3 -m playwright install-deps chromium
python3 build_linux_connector.py
```

The connector opens a visible browser, so it must be run from a Linux desktop
session. The hosted IssueBot worker continues running after the connector exits.

## Render deployment

`render.yaml` provisions a free Flask web service that connects to an external Neon PostgreSQL database. The monitoring process runs beside the website in that single service. Before deploying, configure `DATABASE_URL`, `CREDENTIAL_ENCRYPTION_KEY`, `ADMIN_USERNAME`, and `ADMIN_PASSWORD`.

This free setup is suitable for testing, not dependable 24/7 monitoring: the Render web service sleeps after inactivity and monitoring pauses while it sleeps. Neon persists the relational data independently and wakes its compute when queried. Upgrade to separate paid web and worker services for continuous monitoring.

Render generates `APP_SECRET`. The connected repository should remain private. Official references: [Blueprint specification](https://render.com/docs/blueprint-spec), [background workers](https://render.com/docs/background-workers).

## Vercel deployment

`app.py` is the Vercel WSGI entrypoint. Unlike Render, Vercel does not keep
`worker.py` alive between requests. An Upstash QStash schedule must therefore
send a signed `POST` request to `/api/scan` every five minutes. The endpoint
rejects unsigned calls, prevents overlapping scans with a PostgreSQL advisory
lock, and processes a bounded number of due users per invocation.

1. Import this repository into a Vercel Hobby project.
2. Reuse the existing `DATABASE_URL`, `APP_SECRET`, and
   `CREDENTIAL_ENCRYPTION_KEY` so current accounts and encrypted sessions remain
   usable.
3. Configure `APP_ENV=production`, `ADMIN_USERNAME`, and `ADMIN_PASSWORD`.
4. Copy `QSTASH_CURRENT_SIGNING_KEY` and `QSTASH_NEXT_SIGNING_KEY` from the
   QStash console into Vercel environment variables.
5. Create a QStash schedule with destination
   `https://<vercel-domain>/api/scan`, method `POST`, and cron expression
   `*/5 * * * *`.
6. Confirm `/health`, administrator login, and a successful scheduled scan,
   then stop the previous always-on worker to avoid duplicate applications.

`SCAN_BATCH_SIZE` defaults to `3`, and `SCAN_TIME_BUDGET_SECONDS` defaults to
`220`. Due users not reached in one invocation remain due and are selected by a
later invocation. Vercel serves `public/app.css` directly, while local and
Render runs retain the matching Flask stylesheet route.

## Current limitations

The Drips Wave contributor API is not documented as a stable third-party API. The worker uses the API behavior observed by the existing bot, so Drips frontend/API changes may require maintenance. When a Drips refresh session expires, the user must reconnect with a new one-time code.

Automatic applications must comply with the user's Drips Wave account rules and any repository-specific contribution requirements. Provider usage and charges belong to the API key owner.

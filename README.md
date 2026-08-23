# IssueBot Private Portal

IssueBot is a private, multi-user Drips Wave assistant with two separate sites. The owner manages accounts in an admin-only panel. Each normal user gets a separate portal where they connect Drips, monitor activity, review issue candidates, and explicitly approve applications.

The server source can remain in a private repository. Users receive only the hosted website and the optional compiled connector. Browser-delivered HTML/CSS is inherently visible, and compiled programs can be reverse-engineered, so client-side distribution cannot provide absolute source secrecy.

## Architecture

```text
Admin panel        create, disable, reset, and delete user accounts
User portal        setup, candidates, applications, and on-site activity
PostgreSQL         users, encrypted Drips sessions, issues, and activity
Background worker scans Drips separately for every enabled user
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
- Monitoring only creates a private candidate queue. A user must review scope and explicitly approve each application.

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
5. Enable monitoring.
6. Watch scans, new candidates, accepted assignments, applications, and errors in the dashboard Activity feed.
7. Review candidates, edit the proposed message, and click **Approve and apply**.

Connection codes expire after 15 minutes and can only be used once. The connector does not save the Drips session locally.

## Build the Windows connector

The connector contains no administrator credentials, database access, or service source. Build it on Windows:

```powershell
pip install pyinstaller playwright requests
python build_connector.py
```

Give users only `dist\issuebot-connector.exe` and your HTTPS portal URL.

## Render deployment

`render.yaml` provisions a free Flask web service that connects to an external Neon PostgreSQL database. The monitoring process runs beside the website in that single service. Before deploying, configure `DATABASE_URL`, `CREDENTIAL_ENCRYPTION_KEY`, `ADMIN_USERNAME`, and `ADMIN_PASSWORD`.

This free setup is suitable for testing, not dependable 24/7 monitoring: the Render web service sleeps after inactivity and monitoring pauses while it sleeps. Neon persists the relational data independently and wakes its compute when queried. Upgrade to separate paid web and worker services for continuous monitoring.

Render generates `APP_SECRET`. The connected repository should remain private. Official references: [Blueprint specification](https://render.com/docs/blueprint-spec), [background workers](https://render.com/docs/background-workers).

## Current limitation

The Drips Wave contributor API is not documented as a stable third-party API. The worker uses the API behavior observed by the existing bot, so Drips frontend/API changes may require maintenance. When a Drips refresh session expires, the user must reconnect with a new one-time code.

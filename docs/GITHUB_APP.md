# Registering the PipelineIQ GitHub App

This is a one-time setup, about 10 minutes, done on github.com. You end up with an App that sends
webhooks to your laptop and can post check runs on pull requests. It is the manual part of P3.1.

## 1. Get a webhook URL (smee.io)

GitHub can't reach `localhost`, so a smee.io channel relays webhooks to your laptop.

1. Open <https://smee.io> and click **Start a new channel**.
2. Copy the channel URL, for example `https://smee.io/AbC123xyz`. Keep it private.

You'll start the local relay later (P3.2). Registering the App only needs the URL.

## 2. Create the App

Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**
(<https://github.com/settings/apps/new>). For an organisation, use the org's settings instead.

| Field | Value |
|---|---|
| GitHub App name | `pipelineiq-<yourname>`. The name must be unique on GitHub |
| Homepage URL | The repo URL, or `http://localhost:5173` |
| Callback URL | `http://localhost:8000/auth/github/callback` (used for "Sign in with GitHub") |
| Request user authorization (OAuth) during installation | Leave unticked |
| Webhook → Active | Ticked |
| Webhook URL | Your smee.io channel URL from step 1 |
| Webhook secret | A long random string. Generate one with `openssl rand -hex 32` and save it |

### Repository permissions

| Permission | Access |
|---|---|
| Actions | Read-only |
| Checks | Read and write |
| Contents | Read-only |
| Metadata | Read-only (set automatically) |
| Pull requests | Read and write |

Leave every other permission at **No access**.

### Subscribe to events

Tick these four: **Push**, **Pull request**, **Workflow run**, **Workflow job**.

### Where can this App be installed?

Choose **Only on this account**.

Click **Create GitHub App**.

## 3. Collect the credentials

On the App's settings page:

1. Note the **App ID**, a number near the top.
2. Note the **Client ID**.
3. Under **Client secrets**, click **Generate a new client secret** and copy it. GitHub shows it only once.
4. Under **Private keys**, click **Generate a private key**. A `.pem` file downloads.
   Move it outside the repo, for example `~/.pipelineiq/github-app.pem`, and run `chmod 600` on it.
   The same folder also holds `config` for the pre-push hook (see `tools/pre-push-hook/README.md`).

## 4. Put them in `.env`, never in git

Add these lines to your local `.env`. The P3.1 code reads exactly these names; `.env.example` lists them with placeholders.

```
GITHUB_APP_ID=123456
GITHUB_APP_CLIENT_ID=Iv1.xxxxxxxx
GITHUB_APP_CLIENT_SECRET=...
GITHUB_APP_PRIVATE_KEY_PATH=/Users/<you>/.pipelineiq/github-app.pem
GITHUB_WEBHOOK_SECRET=...
SMEE_URL=https://smee.io/AbC123xyz
```

Don't paste any of these values into chat, issues, logs or LLM prompts.

## 5. Install the App on a test repo

1. On the App page, open **Install App** and choose your account.
2. Pick **Only select repositories** and choose a repo you own, such as a small Node.js test repo.
   Don't install it on the 18 studied open-source repos; they are collected read-only through the API.
3. After installing, note the installation ID in the page URL
   (`.../settings/installations/<installation_id>`).

## 6. Check it works

After you push to the test repo, open the App's **Advanced** tab. The **Recent Deliveries** list
should show a `push` event. It fails with a connection error until the smee relay and the API
are running (P3.2). Receiving the delivery at all means the registration worked.

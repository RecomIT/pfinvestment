# Deploying Bond Amortization to Vercel (GitHub + Neon)

This folder is ready to upload. It holds the same app as the office version, but the data is kept in a
**Neon Postgres** database instead of the `data` folder. Nothing in it is secret: the database password
lives only in Vercel's settings, never in these files.

> **Vercel plan:** the free **Hobby** plan is for **non-commercial personal use only**. Using the app for
> client companies, or as paid work, needs the **Pro** plan (Vercel Fair Use Guidelines). The app works the
> same on both.

---

## 1. Put the folder on GitHub (once)

1. Sign in at github.com, then **New repository**.
   - Name: e.g. `bond-amortization`
   - Choose **Private**
   - Don't add a README
   - Click **Create repository**.
2. On the new repository page, click **uploading an existing file**.
3. Open this folder (`bond_app_vercel`) in Windows Explorer. Select **everything inside it** (Ctrl+A), including `.gitignore` and `.python-version`, and drag it all onto the GitHub page.
4. Click **Commit changes**.
5. Check the file list. `app.py`, `vercel.json` and `requirements.txt` should be at the **top level** of the repository, not inside a sub-folder.

(GitHub Desktop works just as well, if you prefer it.)

## 2. Create the Vercel project

1. Sign in at vercel.com with your GitHub account, then **Add New… → Project**.
2. Choose the `bond-amortization` repository and click **Import**.
   - Framework Preset: leave as detected (FastAPI / Python)
   - Root Directory: `./`
3. Click **Deploy**.

   The first deployment works, but every page will say **"the database is not connected yet"**. That's expected. Continue with step 3.

## 3. Add the Neon database (free)

1. In the project, open **Storage**, then **Create Database**, then **Neon**, then **Continue**.
2. Region: **Singapore (aws-ap-southeast-1)**. The app also runs in Singapore (`sin1`, set in `vercel.json`).
3. Plan: **Free**. Then **Create**, and **Connect** it to this project for all environments: Production, Preview and Development.

   Vercel now adds `DATABASE_URL` and `DATABASE_URL_UNPOOLED` to the project's Environment Variables.
4. Open **Deployments**, then on the latest one click **…** and **Redeploy**.

## 4. Copy your office data and users to the cloud (once, from your PC)

The office app keeps working as before. This step only **copies** its data.

1. In the Vercel project, open **Storage**, then the Neon database, then **.env.local**. Click **Show secret** and copy the value of `DATABASE_URL_UNPOOLED`.
   - It looks like `postgresql://neondb_owner:…@ep-…ap-southeast-1.aws.neon.tech/neondb?sslmode=require`.
   - Keep it private: it is the database password.
2. Open **Command Prompt** in this folder and run:

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set DATABASE_URL=postgresql://...the value you copied...
python manage_cloud.py import-data "D:\AI Implementation ReCom\PF-Investment-Bond\bond_app\data"
```

The import copies everything:
- companies, funds and bonds (with their version numbers)
- all history copies and deleted bonds
- users, with their **current passwords**

At the end it checks that every bond in the cloud matches the office copy. If the cloud database already holds data, it refuses to run; add `--force` only if you really want to copy again.

**Starting fresh instead?** Skip the import and create the first admin:

```
python manage_cloud.py create-admin
```

Other commands (each needs `set DATABASE_URL=...` first in the same window):

| Command | What it does |
|---|---|
| `python manage_cloud.py check` | Counts of everything stored |
| `python manage_cloud.py list-users` | Lists the users |
| `python manage_cloud.py reset-password` | Sets a new password; the user must change it at next login |

## 5. Use it

Open `https://<your-project>.vercel.app` (shown on the Vercel project page) and log in.

- **Address:** anyone with the link can see the login page; everything else needs a login. Share the address only with your team.
- **Security settings:** all of these are set automatically.
  - HTTPS only, and the login cookie works only over HTTPS
  - Logged out after 1 hour idle
  - 5 wrong passwords lock an account for 15 minutes, and there's a limit of 30 login attempts per network address every 15 minutes
  - The `/docs` API page is switched off
  - Dates and "printed on" times are in Bangladesh time
- **Custom domain:** you can add one under **Settings → Domains**.

## 6. Updating later

Make changes in the office version (`bond_app`) and test them there. Then rebuild this folder:

```
cd "D:\AI Implementation ReCom\PF-Investment-Bond\bond_app"
python make_vercel_folder.py
```

Upload the changed files to GitHub the same way as in step 1. Vercel redeploys automatically, and the database tables are kept.

## What is where

| File | Purpose |
|---|---|
| `app.py` | Vercel entry point |
| `vercel.json` | Region Singapore (`sin1`), up to 60 seconds per request |
| `requirements.txt` | Python packages. Vercel installs them, using Python 3.13 (set in `.python-version`). |
| `manage_cloud.py` | Admin commands run from your PC, including the data import |
| `engine/`, `reports/`, `exports/`, `storage/`, `web/` | The app itself, the same code as the office version |
| `storage/pg.py`, `web/pg_auth.py`, `web/cloud.py` | Database storage, logins in the database, and the cloud settings |

**Database tables:** these are created automatically on first start.

| Table | Holds |
|---|---|
| `companies`, `funds`, `bonds` | The data itself |
| `history` | Every save, with who saved it and when |
| `deleted_bonds` | Deleted bonds, so they can be recovered |
| `users` | User accounts |
| `sessions` | Logins. Only a fingerprint of each login token is stored. |
| `login_attempts` | Recent login attempts, for the per-address limit |

# Installing the CEDA Innovation Management Portal

There are three ways to run the portal. The first is the easiest.

## 1. One-step install with Docker (recommended)

Works on Windows, Mac and Linux. You need **Docker Desktop**: https://www.docker.com/products/docker-desktop/

1. Download the source code (green **Code** button on GitHub → *Download ZIP*) and unzip it, or `git clone` it.
2. Start Docker Desktop.
3. Run the installer from the project folder:
   - **Mac or Linux:** `sh scripts/install.sh`
   - **Windows (PowerShell):** `powershell -ExecutionPolicy Bypass -File scripts\install.ps1`
4. When it finishes, open **http://localhost:8000**. Sign in as **admin** with the password the installer prints. It is also stored in the `.env` file.

The installer:
- creates a private `.env` file with new random passwords;
- builds the portal;
- starts it together with a PostgreSQL database; and
- waits until the portal answers.

Run it again at any time to update. Your data and settings are kept.

| Task | Command |
| --- | --- |
| Stop the portal | `docker compose down` (data is kept) |
| Start it again | `docker compose up -d` |
| See what went wrong | `docker compose logs app` |
| Delete everything, including data | `docker compose down -v` |

**Email and SMS.** A fresh install has no email service, so sign-up and reset codes are shown on screen. To send real emails, put `BREVO_API_KEY` and `EMAIL_FROM` (or `SMTP_HOST`, `SMTP_USER` and `SMTP_PASSWORD`) in `.env`. Then change `DEMO_SHOW_CODES` to `false` and run the installer again. For SMS, set `SMS_API_URL` and `SMS_API_KEY`. Every setting is explained in `.env.example`.

## 2. Run without Docker (for developers)

You need Python 3.12.

```
pip install -r requirements-dev.txt
uvicorn backend.app:app --reload
```

Open http://127.0.0.1:8000. The portal uses a local SQLite file in `./data`. If `ADMIN_PASSWORD` is not set, a password for **admin** is generated and printed in the log. Run the tests with `python -m unittest discover -s tests`.

## 3. Deploy to the cloud

- **Render (the demo):** connect the GitHub repository on render.com, choose *Blueprint*, and Render reads `render.yaml`. That creates the web service and a PostgreSQL database.
- **Production:** see [HOSTING.md](HOSTING.md) for the recommended AWS Cape Town setup, the costs, and keeping a copy of the data in Botswana.

## After installing

1. Sign in as **admin** and change the password (Settings).
2. Open **Users** and create staff accounts: evaluators, adjudicators and other administrators. Tick *Make chat consultant* for the people who will answer live chat.
3. Open **Site content**:
   - set the announcement, support details, reply times, chat hours and public holidays;
   - check the FAQs and regulator contacts; and
   - set **Look and feel** to CEDA's colours.
4. Open **Calls / programmes**, **Scoring criteria** and **Application form** to set up the first call.
5. Share the web address. Innovators create their own accounts.

The **User Manual** explains every screen, role by role.

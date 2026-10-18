# Deploying SnapNPlan on AWS

The site runs on one EC2 server at **https://snapnplan.ddns.net**. The
offline exam kit is downloaded from an S3 bucket.

You need: an AWS account, a free No-IP account, the offline exam kit zip
(`SnapNPlan-Offline-Exam-Kit-v2.2.zip`), your Gemini, Groq and Mistral API
keys, and a staff passcode for exam mode. It takes about 30 minutes.

Before you start, push this code to the `main` branch on GitHub. The server
downloads it from there.

## 1. Launch the server

1. Sign in to the AWS console and choose **Asia Pacific (Mumbai)** in the
   region menu, top right.
2. Open **EC2 → Instances → Launch instances** and set:
   - **Name:** `snapnplan`
   - **Application and OS image:** Ubuntu Server 24.04 LTS, 64-bit (x86)
   - **Instance type:** `t3.small`. It has 2 GB of memory; the page
     classifier uses about 0.9 GB.
   - **Key pair:** Create new key pair, name `snapnplan-key`, type RSA,
     format `.pem`. The file downloads; keep it safe.
   - **Network settings:** tick *Allow SSH traffic from My IP*, *Allow HTTPS
     traffic from the internet* and *Allow HTTP traffic from the internet*.
   - **Configure storage:** 20 GiB, gp3.
3. Click **Launch instance**.

## 2. Give it a fixed address

1. Open **EC2 → Elastic IPs → Allocate Elastic IP address** and click
   **Allocate**.
2. Select the new address, then **Actions → Associate Elastic IP address**.
   Choose the `snapnplan` instance and click **Associate**.

Note the address, for example `13.233.10.5`. It stays the same when the server
restarts.

## 3. Point snapnplan.ddns.net at the server

1. Create a free account at [noip.com](https://www.noip.com).
2. In the No-IP dashboard open **DNS Records** and click **Create Hostname**.
3. Enter hostname `snapnplan`, choose the domain `ddns.net`, and leave the
   record type as **A**. The IPv4 address box is filled in with your own
   computer's address: replace it with the Elastic IP. Click **Create**.
4. If `snapnplan` is taken, pick a close name such as `snapnplan-app` and use
   it wherever this guide says snapnplan.ddns.net.

Free No-IP names must be confirmed every 30 days. No-IP emails a link before
the name expires; click it, or the site stops opening.

## 4. Put the offline exam kit on S3

1. In the AWS console, open **CloudShell** (the terminal icon in the top bar),
   still in the Mumbai region.
2. Choose **Actions → Upload file** and upload
   `SnapNPlan-Offline-Exam-Kit-v2.2.zip`.
3. Run these two lines, with a bucket name of your own. Bucket names are
   unique across all of AWS and use lowercase letters, digits and hyphens.

   ```
   curl -fsSLO https://raw.githubusercontent.com/RudraMaharao/snapnplan_cheat_detection/main/deploy/upload-kit.sh
   bash upload-kit.sh SnapNPlan-Offline-Exam-Kit-v2.2.zip snapnplan-downloads-rm
   ```

4. It uploads the kit and the Python installer, makes both downloadable, and
   ends with a line starting `sudo bash /srv/snapnplan/app/deploy/set-env.sh`.
   Copy that line; you need it in step 6.

## 5. Connect to the server

On a Mac, in Terminal:

```
chmod 400 ~/Downloads/snapnplan-key.pem
ssh -i ~/Downloads/snapnplan-key.pem ubuntu@YOUR-ELASTIC-IP
```

On Windows, in PowerShell:

```
icacls "$HOME\Downloads\snapnplan-key.pem" /inheritance:r /grant:r "$($env:USERNAME):(R)"
ssh -i "$HOME\Downloads\snapnplan-key.pem" ubuntu@YOUR-ELASTIC-IP
```

Type `yes` the first time it asks.

## 6. Install SnapNPlan

```
curl -fsSLO https://raw.githubusercontent.com/RudraMaharao/snapnplan_cheat_detection/main/deploy/setup-server.sh
sudo bash setup-server.sh
```

It asks for:

- the web address (press Enter for snapnplan.ddns.net),
- an email for HTTPS certificate notices,
- the staff passcode for exam mode (at least 6 characters),
- your Gemini, Groq and Mistral API keys. Leave out any you do not use.

It then runs by itself for about 10 minutes and ends with
`SnapNPlan is live at https://snapnplan.ddns.net`.

Last, paste the `set-env.sh` line you copied in step 4. The download link now
shows on the offline exam page.

## Updating the site

After you push changes to GitHub, connect to the server and run:

```
sudo bash /srv/snapnplan/app/deploy/update.sh
```

Settings, the database and uploads are kept.

## Day to day

| To | Run |
| --- | --- |
| Watch the app's log | `sudo journalctl -u snapnplan -f` |
| Restart the app | `sudo systemctl restart snapnplan` |
| Change the passcode | `sudo bash /srv/snapnplan/app/deploy/set-env.sh EXAM_PASSCODE=new-passcode` |
| Change an API key | `sudo bash /srv/snapnplan/app/deploy/set-env.sh GROQ_API_KEY=...` |

The HTTPS certificate renews by itself. The database, uploaded pages and exam
records are in `/srv/snapnplan/data`, and the settings in
`/srv/snapnplan/app/.env`. To back everything up, take a snapshot: **EC2 →
Volumes**, select the server's volume, **Actions → Create snapshot**.

## Cost

| Item | Price (Mumbai) |
| --- | --- |
| t3.small server | US$0.0224 an hour, about $16 a month |
| Public IP address | $0.005 an hour, about $3.60 a month |
| 20 GB disk | under $2 a month |
| S3 for the kit | a few cents a month |

New AWS accounts on the free plan get $100 in credits (up to $200), and
t3.small is eligible, so the server runs on credits at first. The free plan
lasts 6 months or until the credits run out. To stop all charges, terminate
the instance, release the Elastic IP and delete the S3 bucket.

## If something goes wrong

**Setup stops at "Getting the HTTPS certificate".** snapnplan.ddns.net does not
point at the server yet. Check the address in No-IP, wait five minutes, then
run `sudo bash /srv/snapnplan/app/deploy/setup-server.sh` again.

**The site shows 502 Bad Gateway.** The app is not running.
`sudo journalctl -u snapnplan -n 50` shows why.

**Making a quiz fails.** Check the API keys, and change them with `set-env.sh`.

**SSH stops connecting.** Your internet address has changed. In **EC2 →
Security groups**, edit the SSH rule of the server's group and set the source
to *My IP* again.

**The offline exam page has no download link.** Run step 4, then paste the line
it prints on the server.

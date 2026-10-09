# Complaint Warrior public pages — EC2 installation

Prepared 6 October 2026 against the public repository commit
fb2db1fc3d80031e5a892c4580df5997c77500cd. These are static HTML pages;
no Flask, Python, package installation, or separate web service is needed.
They have inline CSS and no scripts, analytics, fonts, or external assets.
They have not been installed on your EC2 instance.

## Content to review before publication

The pages use Boris Galitsky as the operator and bgalitsky@hotmail.com as
support/privacy contact. Replace both if another entity operates the service
or you prefer a dedicated support address. Ensure the app name matches OAuth branding.
The contact email appears in all three pages, including mailto links.
The date is a document update date, not a representation that deployment occurred.

The privacy policy describes the inspected code: Gmail tokens and complaint
records, OpenAI processing, optional Twilio calls, AWS hosting, and ngrok when
used. Review actual production providers, logging, retention and backups.
No automatic purge interval, encryption-at-rest implementation, certification,
or response-time guarantee is asserted.

The no-sale, no-advertising, no-generalized-model-training and Limited Use
paragraphs are commitments the operator must adopt and enforce, not findings
that the deployment was audited. Confirm provider account settings/contracts
support these promises. Show the AI/Gmail data-use disclosure near Connect Gmail
and obtain appropriate consent before transferring Gmail data to AI providers.
Implement access control and token encryption at rest before public release;
the inspected Python stores token JSON without application-layer encryption.
EBS encryption, if enabled, does not establish all application-level protections.

The current complaint deletion implementation intentionally retains Gmail
messages and uploaded/local evidence files. The policy explains this and offers
manual deletion by email. Establish a real process to fulfill those requests,
including retained documents, tokens, call-provider data, logs and backups.
Specify any additional jurisdiction-specific rights/contact details required
for your actual operator, users and business. Review the terms for your business.

## 1. Upload and unpack

Download complaint_warrior_legal.zip. Upload it with your usual SCP client to:
/home/ec2-user/complaint_warrior_legal.zip

On EC2:

```bash
unzip /home/ec2-user/complaint_warrior_legal.zip -d /home/ec2-user/cw-legal-upload
cd /home/ec2-user/cw-legal-upload/complaint_warrior_legal
sudo install -d -m 755 /var/www/complaint-warrior-public
sudo install -m 644 privacy.html terms.html about.html /var/www/complaint-warrior-public/
sudo install -d -m 755 /etc/nginx/snippets
sudo install -m 644 legal-pages.conf /etc/nginx/snippets/complaint-warrior-legal.conf
```

Only the HTML pages go into the public directory. Do NOT serve /home/ec2-user/prod
or its credentials, token databases, evidence, scripts or logs as static files.

## 2. Add an include inside the active nginx server block

The repository's production starter defaults to:
/etc/nginx/conf.d/complaint_warrior_apps.conf
The actual path may differ if NGINX_ROUTE_CONFIG is overridden.
Inspect with `sudo nginx -T` locally; do not publish the entire output if it
contains sensitive configuration. Back up the active file before editing:

```bash
sudo cp -p /etc/nginx/conf.d/complaint_warrior_apps.conf /etc/nginx/conf.d/complaint_warrior_apps.conf.before-legal
sudo nano /etc/nginx/conf.d/complaint_warrior_apps.conf
```

Inside the server block for the app's public host, add this line after
`client_max_body_size 50m;` (not outside the server block):

```nginx
include /etc/nginx/snippets/complaint-warrior-legal.conf;
```

This adds exact-match public routes without replacing the current `/` entry
application, `/complaint_warrior/`, OAuth or Twilio routes. Add it to the active
HTTPS server block too if TLS is terminated by nginx instead of ngrok.
If authentication is enforced at a load balancer or proxy, exempt these pages.

Validate and reload only on success:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

If SELinux is enforcing and the pages return 403, inspect nginx's error log;
restore the normal web-content labels where supported with:
`sudo restorecon -Rv /var/www/complaint-warrior-public`
Do not disable SELinux or make private application files world-readable.

## 3. Make it survive your application restart

IMPORTANT: phone_call/scripts/start_all_prod.sh rewrites the nginx config in
write_nginx_routes(). Add the identical include line to the generated server
block in that function, immediately after `client_max_body_size 50m;`.
Edit the actual production copy of the script you run (its deployed path has
not been verified); do not assume the GitHub checkout and deployed tree match.
If you use start_all2.sh or another generator, update that active generator too.
The include has no shell variables, so it can be inserted literally in its heredoc.

Store the change in your normal source control so a future deployment preserves it.
A manual change only to /etc/nginx/conf.d may disappear on the next restart.

## 4. Test through your current public URL

After installation, these URLs should return the pages:

https://foresakenly-figgiest-jazmin.ngrok-free.dev/privacy.html
https://foresakenly-figgiest-jazmin.ngrok-free.dev/terms.html
https://foresakenly-figgiest-jazmin.ngrok-free.dev/about.html

Check in a logged-out/private browser, without Gmail login, ngrok warning,
password prompt or special request header. These are intended routes, not
verified live URLs. A tunnel interstitial can prevent Google from reviewing them.

For a local routing test on EC2:

```bash
curl -I -H 'Host: foresakenly-figgiest-jazmin.ngrok-free.dev' http://127.0.0.1/privacy.html
curl -I -H 'Host: foresakenly-figgiest-jazmin.ngrok-free.dev' http://127.0.0.1/terms.html
curl -I -H 'Host: foresakenly-figgiest-jazmin.ngrok-free.dev' http://127.0.0.1/about.html
```

Look for 200 and Content-Type text/html. Also open each page publicly and verify
its title and navigation. curl success through a tunnel is not a substitute for
a no-login browser check.

## 5. Google production setup

For public production verification, use an HTTPS domain you own and can verify
in Google Search Console. Use a custom domain mapped to the EC2 front end (or a
custom-domain tunnel), rather than relying on the shared ngrok-free.dev domain.
You can keep these same static files and paths when changing the hostname.

In Google Auth Platform > Branding, set:
- Application home page: https://YOUR-VERIFIED-HOST/about.html
- Privacy policy: https://YOUR-VERIFIED-HOST/privacy.html
- Terms of service: https://YOUR-VERIFIED-HOST/terms.html
- Authorized domains: your verified registrable domain.

YOUR-VERIFIED-HOST is a placeholder; substitute your actual owned hostname.
Use a separate production Google Cloud project/client as required by Google's
production guidance. Enable Gmail API and configure its exact HTTPS callback.
If switching the app hostname, update PUBLIC_BASE and the authorized redirect
URI together; keep nginx routing and TLS consistent. Do not change only one side.

Also put visible Privacy and Terms links in the application's entry page and
near Connect Gmail. For a Streamlit view, an example is:

```python
st.markdown('[Privacy Policy](/privacy.html) · [Terms of Service](/terms.html)')
```

Hosting these documents and selecting In production do not by themselves verify
your app. gmail.modify and gmail.readonly are restricted scopes; public deployment
may require restricted-scope verification and a security assessment, subject to
Google's exceptions and review. Request only permissions actually needed.

References:
https://developers.google.com/identity/protocols/oauth2/production-readiness/policy-compliance
https://developers.google.com/identity/protocols/oauth2/policies
https://developers.google.com/terms/api-services-user-data-policy
https://developers.google.com/workspace/gmail/api/auth/scopes
https://nginx.org/en/docs/http/ngx_http_core_module.html#alias

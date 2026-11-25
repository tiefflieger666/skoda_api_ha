# Security policy

## Supported versions
Only the **latest release** receives security fixes. Please update before reporting.

| Version | Supported |
|---|---|
| Latest release | ✅ |
| Older releases | ❌ |

## Reporting a vulnerability
**Do not open a public issue** for a security problem.

Report it privately with GitHub's **private vulnerability reporting**: open the repository's **Security** tab → **Report a vulnerability**. Only the maintainer can read the report.

Please include:
- the integration version and the Home Assistant version;
- what the problem is and what an attacker could do with it;
- steps to reproduce, or a proof of concept.

**Never include your API key, VIN, license plate, GPS position or address**, not even in a private report. Use made-up values; the diagnostics download already removes this data.

## What to expect
- An acknowledgement within **7 days**.
- An assessment and, if confirmed, a fix in a new release as soon as possible. This is a project maintained in spare time, so there is no guaranteed fix date.
- Credit in the release notes, unless you prefer to stay anonymous.

## Scope
In scope: this integration's code, for example the handling and storage of the API key, data in logs, diagnostics, entity states or attributes, and the commands it sends.

Out of scope (report to the respective project instead):
- the MyŠkoda Public API, the MyŠkoda app and Škoda Connect: ŠKODA AUTO a.s.;
- Home Assistant itself: <https://www.home-assistant.io/security/>.

## How the integration handles secrets
- The API key is stored in the Home Assistant config entry (like every Home Assistant integration). It is never written to logs, diagnostics, entity states or attributes.
- The diagnostics download removes the API key, the VIN, the license plate, the vehicle name, the GPS position, the address and the picture URL.
- The integration talks only to `https://public.api.connect.skoda-auto.cz` (and loads the vehicle picture from the URL the API returns).
- If you think your key leaked, revoke it in the MyŠkoda app (Profile → Smart home), create a new one and enter it with Reconfigure.

# Security

## Reporting a vulnerability

Please report security problems **privately** through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue for a security problem.

## How the app protects its data

- The server listens only on `127.0.0.1`, so other computers on the network cannot reach it.
- It refuses requests that come from another web site (`Origin` check) or through another host name (`Host`
  check, against DNS rebinding), so a web page you visit cannot read records or trigger actions.
- The endpoint that holds the Wispr Flow shortcut also requires a header that only the app's own page sends.
- Records, recordings, the Excel workbook, logs and the fleet list stay on the computer. They are excluded from
  this repository by `.gitignore` and must never be committed.
- Dictation with the local Whisper model needs no internet. Dictation with Wispr Flow sends the audio to Wispr's
  cloud service, so use it only where that is acceptable.

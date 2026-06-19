# Web Admin Rules

- Keep gateway tokens in HTTP-only session cookies and proxy all gateway calls through server routes.
- Do not put secrets, tokens, or device private keys in browser storage.
- Do not weaken CSP, CSRF, role checks, or admin token handling.
- UI changes must preserve viewer/operator/owner role separation.

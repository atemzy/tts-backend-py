# TTS backend

The `/tts` endpoint requires a valid Better Auth session. Configure the backend
with the following environment variables. A `.env` file next to `main.py` is
loaded automatically when present; variables already set by the process (such
as production environment variables) take precedence.

- `BETTER_AUTH_SESSION_URL`: the Better Auth session endpoint, for example
  `https://auth.example.com/auth/get-session` when Better Auth uses
  `basePath: "/auth"`.
- `ALLOWED_ORIGINS`: a comma-separated list of exact browser origins allowed to
  call this API, for example `https://app.example.com`.

`/tts` supports two shared rate-limit tiers. Requests without a valid Better
Auth session share a limit of 20 requests per rolling 60-second window and a
100-character text limit. Authenticated requests share a separate limit of 100
requests per rolling 60-second window and a 5,000-character text limit. Excess
requests receive HTTP 429 with a `Retry-After` header; text above the tier's
limit receives a validation error. If Better Auth cannot be reached to verify
a provided session, the request fails with HTTP 503 rather than using the
anonymous tier.

The rate-limit counters are in memory, so they are per process and reset when
the app restarts. Use shared storage such as Redis if running multiple workers
or instances and consistent tier limits are required.

Copy `.env.example` to `.env` for local development and replace the example
values. Do not commit `.env`.

For requests with a session cookie, the backend forwards the incoming `Cookie`
header to the configured session endpoint. Keep the session endpoint on HTTPS.

When the frontend and API are on subdomains of the same site, configure Better
Auth to share its cookie with the narrowest parent domain that contains both
hosts, and include the frontend origin in `trustedOrigins`. For example:

```ts
advanced: {
  crossSubDomainCookies: {
    enabled: true,
    domain: "example.com",
  },
},
trustedOrigins: ["https://app.example.com"],
```

The frontend must include cookies in its API request:

```ts
fetch("https://tts.example.com/tts", {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ text: "Hello", language: "de" }),
});
```

Set `ALLOWED_ORIGINS` to the exact frontend origin; wildcard origins are not
accepted with credentialed CORS.

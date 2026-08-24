# Production hardening

Local defaults are permissive so development is not painful. This page lists
what to change before real traffic reaches the system.

---

## Must do

### 1. A real JWT secret

```bash
JWT_SECRET=$(openssl rand -hex 32)
```

In production the process **will not start** without one, and a placeholder
copied from `env.example` does not count. This is deliberate: without a
verifiable secret, strict authentication disables itself and identity stops
being checked — silently.

### 2. Explicit CORS origins

```bash
CORS_ALLOWED_ORIGINS=https://app.example.com,https://admin.example.com
```

`*` together with credentials is refused — that combination is a CSRF vector.

### 3. Trusted proxy configuration

If you run behind a reverse proxy, either let the proxy overwrite
`X-Forwarded-For` (the shipped Caddyfile does) or set the hop count
explicitly:

```bash
TRUSTED_PROXY_HOPS=1
```

Without this, rate limiting keys on an address the client controls.

### 4. Strict per-user authentication

```bash
STRICT_USER_AUTH=1
```

Rejects token-less requests carrying `?user_id=` on per-user routes. Without
it, an expired browser session keeps reading data by a stale identifier.

---

## Strongly recommended

| Setting | Value | Effect |
|---|---|---|
| `APP_ENV` | `production` | Enables boot-time validators, disables verbose error logging |
| `TESSENT_QUOTA_STRICT` | `on` | Blocks spend when usage cannot be metered, rather than allowing it |
| `LLM_QUOTA_ATOMIC` | default (on) | Prevents a burst of parallel calls from overshooting the cap |
| `RATE_LIMIT_ENABLED` | `true` (default) | Needs Redis to actually enforce |

Rate limiting **fails open**: without Redis it allows everything and logs a
warning. Verify Redis is reachable, or the limits are decorative.

---

## Air-gapped / enterprise mode

```bash
ENTERPRISE_MODE=true
LLM_LOCAL_BASE_URL=http://vllm.internal:8000/v1
```

With this on, the settings validator refuses to start if a cloud provider key
is configured, and every outbound call is checked against the perimeter at
call time as well.

> **Honest limit:** how much answer quality is lost on a local model has not
> been measured. The harness exists; the run has not happened. See
> [What we actually tested](what-we-tested.md).

---

## Verify before you open the door

```bash
python scripts/preflight_check.py     # must exit 0
curl https://your-host/api/v1/health  # every dependency reporting ready
```

Then confirm, deliberately:

- A forged token with `"role": "owner"` gets **403**, not data.
- An unauthenticated request to a per-user route gets **401**.
- Repeated failed logins against one account start returning **429**.
- Your monitoring receives the audit stream.

---

## Related

- [Security](security.md) — what is enforced and where the gaps are
- [Deployment](deployment.md) — the install itself
- [Configuration](configuration.md) — the full variable list

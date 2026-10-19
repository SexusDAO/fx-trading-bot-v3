# Authentication

## Authentication Flow

Authentication is a two-step process:

1. **Initiate sign-in** — run `auth signin --json` to get a login URL, then open the returned `webUrl` in the user's browser (e.g., `open "<webUrl>"` on macOS). The user scans the QR code with the Binance App to authenticate.
2. **Verify** — run `auth verify --qrCodeId <qrCodeId>`. This blocks until the user confirms in the Binance App (or times out after 5 minutes).

---

## `auth signin`

Start the sign-in flow. Open the returned `webUrl` in the browser so the user can scan the QR code with the Binance App.

### Syntax

```bash
baw auth signin [--image] --json
```

### Parameters

| Parameter | Required | Default | Description                                                  |
|-----------|----------|---------|--------------------------------------------------------------|
| `--image` | No       | —       | Display the QR code as an image instead of opening a web URL |

### Example

```bash
baw auth signin --json
```

### Response

```json
{
  "success": true,
  "data": {
    "qrCodeData": "https://app.binance.com/uni-qr/GbxFTARD",
    "qrCodeId": "a191884d-0e05-435b-a887-336bc242fafc",
    "expireAt": "1772767626000",
    "webUrl": "https://web3.binance.com/en/agent-login?expireAt=1772767626000&url=xxx"
  }
}
```

If already logged in:

```json
{
  "success": true,
  "data": { "status": "ALREADY_CONNECTED" }
}
```

---

## `auth verify`

Poll the QR-code scan status and wait for wallet creation to finish. This command blocks until the user confirms in the Binance App and the wallet is ready, or times out after 5 minutes.

### Syntax

```bash
baw auth verify --qrCodeId <qrCodeId> --json
```

### Parameters

| Parameter    | Required | Default | Description                                   |
|--------------|----------|---------|-----------------------------------------------|
| `--qrCodeId` | Yes      | —       | QR code ID from `auth signin --json` response |

### Example

```bash
baw auth verify --qrCodeId a191884d-0e05-435b-a887-336bc242fafc --json
```

### Response

Success:

```json
{
  "success": true,
  "data": {
    "status": "SUCCESS"
  }
}
```

Timeout, rejection, or other failure:

```json
{
  "success": false,
  "error": {
    "code": 10002001,
    "name": "AUTH_REJECTED",
    "suggestion": "Run 'auth signin' again"
  }
}
```

---

## `auth signout`

Sign out of the wallet and clear the local session.

### Syntax

```bash
baw auth signout --json
```

### Parameters

No command-specific parameters.

### Example

```bash
baw auth signout --json
```

### Response

```json
{
  "success": true,
  "data": { "status": "LOGGED_OUT" }
}
```
